import json,time,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from src.uia_service import ReplicaUIADriver,WeChatUIAService
rows=[]
for i in range(3):
 t=time.perf_counter(); r={"run":i+1}
 try:
  s=WeChatUIAService(ReplicaUIADriver(),timeout=5); s.connect(); c=s.get_current_chat(); chats=s.get_chats()
  r.update(status="PASS",current_chat=c,chat_count=len(chats))
 except Exception as e: r.update(status="FAIL",error=type(e).__name__+":"+str(e))
 r["duration_ms"]=round((time.perf_counter()-t)*1000,1); rows.append(r)
out={"runs":rows,"success_count":sum(x["status"]=="PASS" for x in rows),"failure_count":sum(x["status"]!="PASS" for x in rows)}
p=Path("docs/evidence/live_e2e_regression.json"); p.parent.mkdir(parents=True,exist_ok=True); p.write_text(json.dumps(out,ensure_ascii=False,indent=2),encoding="utf-8"); print(json.dumps(out,ensure_ascii=False,indent=2))
