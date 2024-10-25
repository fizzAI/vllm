# WIP WINDOWS FORK DOCS
`uv tool install --from git+https://github.com/fizzAI/vllm@window vllm --with winloop --with xformers --with $env:TRITON_WHEEL --with $env:TORCH_WHEEL --with setuptools` will install it + all the deps.

the wheel paths are just placeholders, you'll need to find the right ones for your python version-- `uv` doesnt like installing the right torch version with cuda, so download the wheel from the pytorch index repo. triton for windows is available at https://github.com/woct0rdho/triton-windows.