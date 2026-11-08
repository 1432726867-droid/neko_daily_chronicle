"""neko_daily_chronicle 的测试包。

测试只依赖标准库，因此在没有 N.E.K.O SDK 的环境里也能直接跑：

    python -m unittest discover -s tests -t . -v
"""

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
