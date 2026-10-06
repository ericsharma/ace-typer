"""Emergency: send a neutral packet (no buttons) to every connected controller."""
import json, threading, time, socketio
from .send import NEUTRAL
sio = socketio.Client(); ev = threading.Event(); st = {}
@sio.on("state")
def s(d): st.update(d); ev.set()
sio.connect("http://127.0.0.1:8170"); sio.emit("state"); ev.wait(5)
for k in st:
    for _ in range(3):
        sio.emit("input", json.dumps([int(k), NEUTRAL])); time.sleep(0.05)
    print("released controller", k, st[k].get("state"))
time.sleep(0.3); sio.disconnect()
