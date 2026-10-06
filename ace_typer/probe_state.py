import json, threading, socketio
sio = socketio.Client(); ev = threading.Event(); out = {}
@sio.on("state")
def s(d): out.update(d); ev.set()
sio.connect("http://127.0.0.1:8170"); sio.emit("state"); ev.wait(5)
for k, v in out.items():
    print(k, v.get("state"), v.get("last_connection"), (v.get("errors") or "")[-300:].replace("\n", " | "))
print("controllers:", len(out)); sio.disconnect()
