import pytest
pytestmark = pytest.mark.live_wechat

def test_live_uia_placeholder():
    """真实 UIA 由显式手工命令运行；默认不启动微信。"""
    pytest.skip("需要显式配置 live UIA driver")
