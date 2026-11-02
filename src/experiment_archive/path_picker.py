"""Native dialogs run in a separate process, keeping Tk on its main thread."""
import json
import sys
from pathlib import Path


def main():
    import tkinter as tk
    from tkinter import filedialog
    kind, initial = sys.argv[1:3]
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    path = Path(initial) if initial else Path.home()
    directory = path if path.is_dir() else path.parent
    options = {"parent": root, "initialdir": str(directory), "title": "选择归档路径"}
    try:
        selected = (filedialog.askdirectory(**options, mustexist=True) if kind == "directory"
                    else filedialog.askopenfilename(**options))
        print(json.dumps({"path": selected or None}, ensure_ascii=True))
    finally:
        root.destroy()


if __name__ == "__main__":
    main()
