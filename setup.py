import importlib.util
import io
import logging
import os
import re
import subprocess
import sys
from pathlib import Path
from shutil import which
from typing import Dict, List

import torch
from packaging.version import Version, parse
from setuptools import Extension, find_packages, setup
from setuptools.command.build_ext import build_ext
from setuptools_scm import get_version
from torch.utils.cpp_extension import CUDA_HOME


def load_module_from_path(module_name, path):
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


ROOT_DIR = os.path.dirname(__file__)
logger = logging.getLogger(__name__)

# cannot import envs directly because it depends on vllm,
#  which is not installed yet
envs = load_module_from_path('envs', os.path.join(ROOT_DIR, 'vllm', 'envs.py'))

VLLM_TARGET_DEVICE = envs.VLLM_TARGET_DEVICE

if not sys.platform.startswith("linux"):
    logger.warning(
        "vLLM only officially supports Linux platform (including WSL). "
        "Building on %s, so vLLM may not be able to run correctly",
        sys.platform)
    if sys.platform in "win32":
        logger.warning("Running on Windows. This is not officially supported, but vLLM should still work.")
        logger.warning("Assuming CUDA backend will be used, others are not supported for Windows.")
        VLLM_TARGET_DEVICE = "empty"
    else:
        VLLM_TARGET_DEVICE = "empty"

MAIN_CUDA_VERSION = "12.1"


def is_sccache_available() -> bool:
    return which("sccache") is not None


def is_ccache_available() -> bool:
    return which("ccache") is not None


def is_ninja_available() -> bool:
    return which("ninja") is not None


def remove_prefix(text, prefix):
    if text.startswith(prefix):
        return text[len(prefix):]
    return text


class CMakeExtension(Extension):

    def __init__(self, name: str, cmake_lists_dir: str = '.', **kwa) -> None:
        super().__init__(name, sources=[], py_limited_api=True, **kwa)
        self.cmake_lists_dir = os.path.abspath(cmake_lists_dir)


class cmake_build_ext(build_ext):
    # A dict of extension directories that have been configured.
    did_config: Dict[str, bool] = {}

    #
    # Determine number of compilation jobs and optionally nvcc compile threads.
    #
    def compute_num_jobs(self):
        # `num_jobs` is either the value of the MAX_JOBS environment variable
        # (if defined) or the number of CPUs available.
        num_jobs = envs.MAX_JOBS
        if num_jobs is not None:
            num_jobs = int(num_jobs)
            logger.info("Using MAX_JOBS=%d as the number of jobs.", num_jobs)
        else:
            try:
                # os.sched_getaffinity() isn't universally available, so fall
                #  back to os.cpu_count() if we get an error here.
                num_jobs = len(os.sched_getaffinity(0))
            except AttributeError:
                num_jobs = os.cpu_count()

        nvcc_threads = None
        if _is_cuda() and get_nvcc_cuda_version() >= Version("11.2"):
            # `nvcc_threads` is either the value of the NVCC_THREADS
            # environment variable (if defined) or 1.
            # when it is set, we reduce `num_jobs` to avoid
            # overloading the system.
            nvcc_threads = envs.NVCC_THREADS
            if nvcc_threads is not None:
                nvcc_threads = int(nvcc_threads)
                logger.info(
                    "Using NVCC_THREADS=%d as the number of nvcc threads.",
                    nvcc_threads)
            else:
                nvcc_threads = 1
            num_jobs = max(1, num_jobs // nvcc_threads)

        return num_jobs, nvcc_threads

    #
    # Perform cmake configuration for a single extension.
    #
    def configure(self, ext: CMakeExtension) -> None:
        # If we've already configured using the CMakeLists.txt for
        # this extension, exit early.
        if ext.cmake_lists_dir in cmake_build_ext.did_config:
            return

        cmake_build_ext.did_config[ext.cmake_lists_dir] = True

        # Select the build type.
        # Note: optimization level + debug info are set by the build type
        default_cfg = "Debug" if self.debug else "RelWithDebInfo"
        cfg = envs.CMAKE_BUILD_TYPE or default_cfg

        cmake_args = [
            '-DCMAKE_BUILD_TYPE={}'.format(cfg),
            '-DVLLM_TARGET_DEVICE={}'.format(VLLM_TARGET_DEVICE),
        ]

        verbose = envs.VERBOSE
        if verbose:
            cmake_args += ['-DCMAKE_VERBOSE_MAKEFILE=ON']

        if is_sccache_available():
            cmake_args += [
                '-DCMAKE_C_COMPILER_LAUNCHER=sccache',
                '-DCMAKE_CXX_COMPILER_LAUNCHER=sccache',
                '-DCMAKE_CUDA_COMPILER_LAUNCHER=sccache',
                '-DCMAKE_HIP_COMPILER_LAUNCHER=sccache',
            ]
        elif is_ccache_available():
            cmake_args += [
                '-DCMAKE_C_COMPILER_LAUNCHER=ccache',
                '-DCMAKE_CXX_COMPILER_LAUNCHER=ccache',
                '-DCMAKE_CUDA_COMPILER_LAUNCHER=ccache',
                '-DCMAKE_HIP_COMPILER_LAUNCHER=ccache',
            ]

        # Pass the python executable to cmake so it can find an exact
        # match.
        cmake_args += ['-DVLLM_PYTHON_EXECUTABLE={}'.format(sys.executable)]

        # Pass the python path to cmake so it can reuse the build dependencies
        # on subsequent calls to python.
        cmake_args += ['-DVLLM_PYTHON_PATH={}'.format(":".join(sys.path))]

        # Override the base directory for FetchContent downloads to $ROOT/.deps
        # This allows sharing dependencies between profiles,
        # and plays more nicely with sccache.
        # To override this, set the FETCHCONTENT_BASE_DIR environment variable.
        fc_base_dir = os.path.join(ROOT_DIR, ".deps")
        fc_base_dir = os.environ.get("FETCHCONTENT_BASE_DIR", fc_base_dir)
        cmake_args += ['-DFETCHCONTENT_BASE_DIR={}'.format(fc_base_dir)]

        #
        # Setup parallelism and build tool
        #
        num_jobs, nvcc_threads = self.compute_num_jobs()

        if nvcc_threads:
            cmake_args += ['-DNVCC_THREADS={}'.format(nvcc_threads)]

        if is_ninja_available():
            build_tool = ['-G', 'Ninja']
            cmake_args += [
                '-DCMAKE_JOB_POOL_COMPILE:STRING=compile',
                '-DCMAKE_JOB_POOLS:STRING=compile={}'.format(num_jobs),
            ]
        else:
            # Default build tool to whatever cmake picks.
            build_tool = []
        subprocess.check_call(
            ['cmake', ext.cmake_lists_dir, *build_tool, *cmake_args],
            cwd=self.build_temp)

    def build_extensions(self) -> None:
        # Ensure that CMake is present and working
        try:
            subprocess.check_output(['cmake', '--version'])
        except OSError as e:
            raise RuntimeError('Cannot find CMake executable') from e

        # Create build directory if it does not exist.
        if not os.path.exists(self.build_temp):
            os.makedirs(self.build_temp)

        targets = []
        target_name = lambda s: remove_prefix(remove_prefix(s, "vllm."),
                                              "vllm_flash_attn.")
        # Build all the extensions
        for ext in self.extensions:
            self.configure(ext)
            targets.append(target_name(ext.name))

        num_jobs, _ = self.compute_num_jobs()

        build_args = [
            "--build",
            ".",
            f"-j={num_jobs}",
            *[f"--target={name}" for name in targets],
        ]

        subprocess.check_call(["cmake", *build_args], cwd=self.build_temp)

        # Install the libraries
        for ext in self.extensions:
            # Install the extension into the proper location
            outdir = Path(self.get_ext_fullpath(ext.name)).parent.absolute()

            # Skip if the install directory is the same as the build directory
            if outdir == self.build_temp:
                continue

            # CMake appends the extension prefix to the install path,
            # and outdir already contains that prefix, so we need to remove it.
            prefix = outdir
            for i in range(ext.name.count('.')):
                prefix = prefix.parent

            # prefix here should actually be the same for all components
            install_args = [
                "cmake", "--install", ".", "--prefix", prefix, "--component",
                target_name(ext.name)
            ]
            subprocess.check_call(install_args, cwd=self.build_temp)

    def run(self):
        # First, run the standard build_ext command to compile the extensions
        super().run()

        # copy vllm/vllm_flash_attn/*.py from self.build_lib to current
        # directory so that they can be included in the editable build
        import glob
        files = glob.glob(
            os.path.join(self.build_lib, "vllm", "vllm_flash_attn", "*.py"))
        for file in files:
            dst_file = os.path.join("vllm/vllm_flash_attn",
                                    os.path.basename(file))
            print(f"Copying {file} to {dst_file}")
            self.copy_file(file, dst_file)


def _no_device() -> bool:
    return VLLM_TARGET_DEVICE == "empty"

def _is_windows() -> bool:
    return VLLM_TARGET_DEVICE == "windows"

def _is_cuda() -> bool:
    has_cuda = torch.version.cuda is not None
    return (VLLM_TARGET_DEVICE == "cuda" and has_cuda
            and not (_is_neuron() or _is_tpu()))


def _is_hip() -> bool:
    return (VLLM_TARGET_DEVICE == "cuda"
            or VLLM_TARGET_DEVICE == "rocm") and torch.version.hip is not None


def _is_neuron() -> bool:
    torch_neuronx_installed = True
    try:
        subprocess.run(["neuron-ls"], capture_output=True, check=True)
    except (FileNotFoundError, PermissionError, subprocess.CalledProcessError):
        torch_neuronx_installed = False
    return torch_neuronx_installed or VLLM_TARGET_DEVICE == "neuron"


def _is_tpu() -> bool:
    return VLLM_TARGET_DEVICE == "tpu"


def _is_cpu() -> bool:
    return VLLM_TARGET_DEVICE == "cpu"


def _is_openvino() -> bool:
    return VLLM_TARGET_DEVICE == "openvino"


def _is_xpu() -> bool:
    return VLLM_TARGET_DEVICE == "xpu"


def _build_custom_ops() -> bool:
    return _is_cuda() or _is_hip() or _is_cpu()


def get_hipcc_rocm_version():
    # Run the hipcc --version command
    result = subprocess.run(['hipcc', '--version'],
                            stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT,
                            text=True)

    # Check if the command was executed successfully
    if result.returncode != 0:
        print("Error running 'hipcc --version'")
        return None

    # Extract the version using a regular expression
    match = re.search(r'HIP version: (\S+)', result.stdout)
    if match:
        # Return the version string
        return match.group(1)
    else:
        print("Could not find HIP version in the output")
        return None


def get_neuronxcc_version():
    import sysconfig
    site_dir = sysconfig.get_paths()["purelib"]
    version_file = os.path.join(site_dir, "neuronxcc", "version",
                                "__init__.py")

    # Check if the command was executed successfully
    with open(version_file, "rt") as fp:
        content = fp.read()

    # Extract the version using a regular expression
    match = re.search(r"__version__ = '(\S+)'", content)
    if match:
        # Return the version string
        return match.group(1)
    else:
        raise RuntimeError("Could not find Neuron version in the output")


def get_nvcc_cuda_version() -> Version:
    """Get the CUDA version from nvcc.

    Adapted from https://github.com/NVIDIA/apex/blob/8b7a1ff183741dd8f9b87e7bafd04cfde99cea28/setup.py
    """
    assert CUDA_HOME is not None, "CUDA_HOME is not set"
    nvcc_output = subprocess.check_output([CUDA_HOME + "/bin/nvcc", "-V"],
                                          universal_newlines=True)
    output = nvcc_output.split()
    release_idx = output.index("release") + 1
    nvcc_cuda_version = parse(output[release_idx].split(",")[0])
    return nvcc_cuda_version


def get_path(*filepath) -> str:
    return os.path.join(ROOT_DIR, *filepath)


def get_vllm_version() -> str:
    version = get_version(
        write_to="vllm/_version.py",  # TODO: move this to pyproject.toml
    )

    sep = "+" if "+" not in version else "."  # dev versions might contain +

    if _no_device():
        if envs.VLLM_TARGET_DEVICE == "empty":
            version += f"{sep}empty"
    elif _is_cuda():
        cuda_version = str(get_nvcc_cuda_version())
        if cuda_version != MAIN_CUDA_VERSION:
            cuda_version_str = cuda_version.replace(".", "")[:3]
            # skip this for source tarball, required for pypi
            if "sdist" not in sys.argv:
                version += f"{sep}cu{cuda_version_str}"
    elif _is_hip():
        # Get the HIP version
        hipcc_version = get_hipcc_rocm_version()
        if hipcc_version != MAIN_CUDA_VERSION:
            rocm_version_str = hipcc_version.replace(".", "")[:3]
            version += f"{sep}rocm{rocm_version_str}"
    elif _is_neuron():
        # Get the Neuron version
        neuron_version = str(get_neuronxcc_version())
        if neuron_version != MAIN_CUDA_VERSION:
            neuron_version_str = neuron_version.replace(".", "")[:3]
            version += f"{sep}neuron{neuron_version_str}"
    elif _is_openvino():
        version += f"{sep}openvino"
    elif _is_tpu():
        version += f"{sep}tpu"
    elif _is_cpu():
        version += f"{sep}cpu"
    elif _is_xpu():
        version += f"{sep}xpu"
    else:
        raise RuntimeError("Unknown runtime environment")

    return version


def read_readme() -> str:
    """Read the README file if present."""
    p = get_path("README.md")
    if os.path.isfile(p):
        return io.open(get_path("README.md"), "r", encoding="utf-8").read()
    else:
        return ""


def get_requirements() -> List[str]:
    """Get Python package dependencies from requirements.txt."""

    def _read_requirements(filename: str) -> List[str]:
        with open(get_path(filename)) as f:
            requirements = f.read().strip().split("\n")
        resolved_requirements = []
        for line in requirements:
            if line.startswith("-r "):
                resolved_requirements += _read_requirements(line.split()[1])
            elif line.startswith("--"):
                continue
            else:
                resolved_requirements.append(line)
        return resolved_requirements

    if _no_device() or _is_windows():
        requirements = _read_requirements("requirements-cuda.txt")
    elif _is_cuda():
        requirements = _read_requirements("requirements-cuda.txt")
        cuda_major, cuda_minor = torch.version.cuda.split(".")
        modified_requirements = []
        for req in requirements:
            if ("vllm-flash-attn" in req
                    and not (cuda_major == "12" and cuda_minor == "1")):
                # vllm-flash-attn is built only for CUDA 12.1.
                # Skip for other versions.
                continue
            modified_requirements.append(req)
        requirements = modified_requirements
    elif _is_hip():
        requirements = _read_requirements("requirements-rocm.txt")
    elif _is_neuron():
        requirements = _read_requirements("requirements-neuron.txt")
    elif _is_openvino():
        requirements = _read_requirements("requirements-openvino.txt")
    elif _is_tpu():
        requirements = _read_requirements("requirements-tpu.txt")
    elif _is_cpu():
        requirements = _read_requirements("requirements-cpu.txt")
    elif _is_xpu():
        requirements = _read_requirements("requirements-xpu.txt")
    else:
        raise ValueError(
            "Unsupported platform, please use CUDA, ROCm, Neuron, "
            "OpenVINO, or CPU.")
    
    if _is_windows():
        requirements.append("winloop") # FIXME: remove this once we have a better way to handle Windows

    return requirements


ext_modules = []

if _is_cuda() or _is_hip():
    ext_modules.append(CMakeExtension(name="vllm._moe_C"))

if _is_hip():
    ext_modules.append(CMakeExtension(name="vllm._rocm_C"))

if _is_cuda():
    ext_modules.append(
        CMakeExtension(name="vllm.vllm_flash_attn.vllm_flash_attn_c"))

if _build_custom_ops():
    ext_modules.append(CMakeExtension(name="vllm._C"))

package_data = {
    "vllm": ["py.typed", "model_executor/layers/fused_moe/configs/*.json"]
}
if envs.VLLM_USE_PRECOMPILED:
    ext_modules = []
    package_data["vllm"].append("*.so")

if _no_device():
    ext_modules = []

setup(
    name="vllm",
    version=get_vllm_version(),
    author="vLLM Team",
    license="Apache 2.0",
    description=("A high-throughput and memory-efficient inference and "
                 "serving engine for LLMs"),
    long_description=read_readme(),
    long_description_content_type="text/markdown",
    url="https://github.com/vllm-project/vllm",
    project_urls={
        "Homepage": "https://github.com/vllm-project/vllm",
        "Documentation": "https://vllm.readthedocs.io/en/latest/",
    },
    classifiers=[
        "Programming Language :: Python :: 3.8",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
        "License :: OSI Approved :: Apache Software License",
        "Intended Audience :: Developers",
        "Intended Audience :: Information Technology",
        "Intended Audience :: Science/Research",
        "Topic :: Scientific/Engineering :: Artificial Intelligence",
        "Topic :: Scientific/Engineering :: Information Analysis",
    ],
    packages=find_packages(exclude=("benchmarks", "csrc", "docs", "examples",
                                    "tests*")),
    python_requires=">=3.8",
    install_requires=get_requirements(),
    ext_modules=ext_modules,
    extras_require={
        "tensorizer": ["tensorizer>=2.9.0"],
        "audio": ["librosa", "soundfile"]  # Required for audio processing
    },
    cmdclass={"build_ext": cmake_build_ext} if len(ext_modules) > 0 else {},
    package_data=package_data,
    entry_points={
        "console_scripts": [
            "vllm=vllm.scripts:main",
        ],
    },
)
