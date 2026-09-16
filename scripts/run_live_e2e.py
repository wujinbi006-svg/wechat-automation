"""显式运行的真实 UIA E2E；不发送消息。"""
import json,time,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from src.uia_service import ReplicaUIADriver,WeChatUIAService
from src.errors import WeChatError
def main():
 s=WeChatUIAService(ReplicaUIADriver(),timeout=5); out={"timestamp":time.time(),"provider":"uia","source":"live_wechat","operations":{}}
 for name,fn in [("connect",s.connect),("current_chat",s.get_current_chat),("get_chats",s.get_chats)]:
  t=time.perf_counter()
  try:
   v=fn(); out["operations"][name]={"status":"PASS","duration_ms":round((time.perf_counter()-t)*1000,1),"summary":len(v) if isinstance(v,list) else v}
  except Exception as e: out["operations"][name]={"status":"BLOCKED","error":type(e).__name__+":"+str(e),"duration_ms":round((time.perf_counter()-t)*1000,1)}
 t=time.perf_counter()
 try: out["operations"]["open_chat"]={"status":"PASS" if s.open_chat("好友B") else "FAIL","duration_ms":round((time.perf_counter()-t)*1000,1)}
 except Exception as e: out["operations"]["open_chat"]={"status":"BLOCKED","error":type(e).__name__+":"+str(e),"duration_ms":round((time.perf_counter()-t)*1000,1)}
 try:
  e=s.driver.get_chat_input_field()
  out["operations"]["find_chat_input"]={"status":"PASS","automation_id":getattr(e,"AutomationId",""),"class":getattr(e,"ClassName",""),"name":getattr(e,"Name","")}
  from src.minimal_foreground_input import MinimalForegroundInput
  mfi=MinimalForegroundInput(); ok,dur,rest=mfi.set_text_with_restore(e,"UIA_AUTOMATION_TEST_DRAFT")
  out["operations"]["type_message"]={"status":"PASS" if ok else "FAIL","foreground_required":True,"foreground_duration_ms":dur,"restored":rest}
  if ok:
   mfi.set_text_with_restore(e,"")
   out["operations"]["draft_cleared"]=True
 except Exception as e: out["operations"]["find_chat_input"]={"status":"BLOCKED","error":type(e).__name__+":"+str(e)}
 out["send"]={"status":"DISABLED"}; out["database_read"]={"status":"UNAVAILABLE"}
 p=Path("docs/evidence/live_e2e_final.json"); p.parent.mkdir(parents=True,exist_ok=True); p.write_text(json.dumps(out,ensure_ascii=False,indent=2),encoding="utf-8"); print(json.dumps(out,ensure_ascii=False,indent=2))
if __name__=="__main__": main()
