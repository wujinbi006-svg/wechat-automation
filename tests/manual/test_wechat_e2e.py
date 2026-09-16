"""真实微信 E2E 手工入口。默认不由 pytest 收集，也不会自动发送消息。"""
import json
from pathlib import Path
def main():
    result={"status":"NOT_RUN","send_attempted":False,
            "reason":"需要用户显式在已登录微信环境中运行手工 UIA driver"}
    p=Path(__file__).parents[2]/"docs"/"evidence"/"e2e_smoke_result.json"
    p.parent.mkdir(parents=True,exist_ok=True); p.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps(result,ensure_ascii=False,indent=2))
if __name__=="__main__": main()
