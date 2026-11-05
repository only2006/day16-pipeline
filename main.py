"""兼容入口：真正的 CLI 在 runner.py。

uv init 生成的模板本来该删掉，这里改成薄转发，
免得 `uv run main.py` 打印一句无关的 Hello 让人误以为是程序入口。
"""

from runner import main

if __name__ == "__main__":
    raise SystemExit(main())
