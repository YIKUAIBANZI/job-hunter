#!/usr/bin/env python3
"""liepin：浏览器点击 + 岗位身份校验 + Jev 分流。先运行 --dry-run。"""
from platform_runner import main as run_platform

CITY_CODES = {
    "全国": "000",
    "北京": "010",
    "上海": "020",
    "广州": "050020",
    "深圳": "050090",
    "杭州": "070020",
    "南京": "070040",
    "成都": "280020",
    "武汉": "180020",
    "苏州": "070060",
    "天津": "030",
    "重庆": "040",
}


def main(argv=None):
    return run_platform('liepin', CITY_CODES, argv)


if __name__ == '__main__':
    main()
