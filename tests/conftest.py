"""从项目根运行测试时，平铺模块仍可直接导入。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
