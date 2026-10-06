import time, socketio
sio = socketio.Client()
got = {}
sio.on("create_pro_controller", lambda i: got.setdefault("index", i))
sio.on("error", lambda e: got.setdefault("error", e))
sio.connect("http://127.0.0.1:8170")
sio.emit("web_create_pro_controller")
time.sleep(5)
print("created:", got)
sio.disconnect()  # the web app removes this session's controller
