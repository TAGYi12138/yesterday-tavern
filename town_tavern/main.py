"""启动入口。

用法:
    export DEEPSEEK_API_KEY=你的key
    python -m town_tavern.main
"""
from .cli import run

if __name__ == "__main__":
    run()
