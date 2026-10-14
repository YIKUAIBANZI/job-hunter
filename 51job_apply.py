#!/usr/bin/env python3
"""51job：浏览器点击 + 岗位身份校验 + Jev 分流。先运行 --dry-run。"""
from platform_runner import main as run_platform

CITY_CODES = {
    "北京": "010000",
    "上海": "020000",
    "广州": "030200",
    "深圳": "040000",
    "杭州": "080200",
    # 更多城市码可从 we.51job.com 城市选择器抓取后补充
}


def main(argv=None):
    return run_platform('51job', CITY_CODES, argv)


if __name__ == '__main__':
    main()
