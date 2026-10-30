import sys

from quickdeck.cli import main

if __name__ == "__main__":
    # 冻结版没有外部 Python：插件宿主会让程序自身以 --plugin-runner 模式启动。
    if len(sys.argv) >= 3 and sys.argv[1] == "--plugin-runner":
        from quickdeck import plugin_runner

        sys.argv = [sys.argv[0], sys.argv[2]]
        raise SystemExit(plugin_runner.main())
    raise SystemExit(main())
