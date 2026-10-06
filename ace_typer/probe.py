from ace_typer.send import Controller
try:
    c = Controller(); print("connected controller index", c.index); c.close()
except RuntimeError as e:
    print("RESULT:", e)
