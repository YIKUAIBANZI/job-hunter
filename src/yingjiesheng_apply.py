#!/usr/bin/env python3
"""应届生求职网：原生点击、岗位身份核验与 Jev 分流；首单待校准。"""
from platform_runner import main as run_platform

CITY_CODES = {
    '北京': '010000',
    '上海': '020000',
    '广州': '030200',
    '深圳': '040000',
    '杭州': '080200',
}


def main(argv=None):
    return run_platform('yingjiesheng', CITY_CODES, argv)


if __name__ == '__main__':
    main()
