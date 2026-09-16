import argparse, json
from src.providers import DatabaseDataProvider
from src.service_api import WeChatService
from src.uia_service import WeChatUIAService
def main():
 p=argparse.ArgumentParser(prog="wechat"); p.add_argument("command",choices=["status","capabilities","chats","current-chat","search","open-chat","type-message"]); p.add_argument("value",nargs="?")
 a=p.parse_args(); s=WeChatService(DatabaseDataProvider(),WeChatUIAService())
 if a.command=="status": out=s.status()
 elif a.command=="capabilities": out=s.status()["capabilities"]
 elif a.command=="chats": out=s.ui_chats()
 elif a.command=="current-chat": out=s.ui_current_chat()
 elif a.command in ("search","open-chat"): out={"success":False,"error":{"code":"UIA_UNAVAILABLE","message":"请在手工集成模式注入 UIA driver"}}
 elif a.command=="type-message": out={"success":False,"error":{"code":"FOREGROUND_REQUIRED","message":"需要显式前台操作；CLI 不自动抢占前台"}}
 print(json.dumps(out,ensure_ascii=False,indent=2)); return 0
if __name__=="__main__": raise SystemExit(main())
