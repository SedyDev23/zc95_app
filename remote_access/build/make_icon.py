"""Write zc95.ico (the exe's icon) from the app's own icon drawing, at the standard Windows sizes.

Run by build_exe.bat; you don't normally need to run it yourself.
Usage:  python make_icon.py <output.ico>
"""
import base64
import os
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))   # remote_access, for zc95_lua_manager and lib/

from zc95_lua_manager import icon_png  # noqa: E402

SIZES = (16, 24, 32, 48, 64, 128, 256)


def main(out_path):
  # A .ico can hold PNG images directly (Windows Vista and later)
  images = [base64.b64decode(icon_png(size)) for size in SIZES]
  header = struct.pack("<HHH", 0, 1, len(images))
  offset = len(header) + 16 * len(images)
  entries = b""
  for size, data in zip(SIZES, images):
    dim = 0 if size >= 256 else size   # 0 means 256 in the .ico format
    entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(data), offset)
    offset += len(data)
  with open(out_path, "wb") as f:
    f.write(header + entries + b"".join(images))
  print("wrote " + out_path)


if __name__ == "__main__":
  main(sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "zc95.ico"))
