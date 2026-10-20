"""SH2 binary investigation through the installed IDALib, without execution."""
import idapro
from pathlib import Path
import argparse
import shutil
import sys
import ida_auto
import ida_bytes
import ida_funcs
import ida_ida
import ida_idp
import ida_segment
import idautils
import idc
from fls.core.path_compat import WORK

sys.stdout.reconfigure(encoding="utf-8")
parser = argparse.ArgumentParser()
parser.add_argument("--functions", nargs="*", default=[])
parser.add_argument("--xrefs", nargs="*", default=[])
args = parser.parse_args()
source = WORK / "extracted/steam/1ST.BIN"
binary = WORK / "analysis/Saturn_1ST.BIN"
if not binary.exists():
    shutil.copy2(source, binary)
database = Path(str(binary) + ".i64")
rc = idapro.open_database(str(database if database.exists() else binary), False,
                         args=None if database.exists() else '-pSH3 -b600400 -T"Binary file"')
if rc:
    raise RuntimeError(f"Database open failed: {rc}")
try:
    print("Processor", ida_idp.get_idp_name())
    segments = [(hex(ea), hex(ida_segment.getseg(ea).end_ea)) for ea in idautils.Segments()]
    print("Segments", segments)
    if ida_ida.inf_get_min_ea() != 0x06004000:
        raise RuntimeError("Unexpected Saturn load address")
    ida_ida.inf_set_be(True)
    ida_auto.auto_make_proc(0x06004000)
    ida_auto.auto_make_proc(0x06004094)
    ida_auto.auto_wait()
    for spec in args.xrefs:
        ea = int(spec, 0)
        print("Xrefs", spec, [(hex(x.frm), idc.get_func_name(x.frm)) for x in idautils.XrefsTo(ea)])
    for spec in args.functions:
        ea = int(spec, 0)
        ida_auto.auto_make_proc(ea)
        ida_auto.auto_wait()
        func = ida_funcs.get_func(ea)
        if func is None:
            print("No function", spec)
            continue
        text = "\n".join(f"{head:08X} {idc.generate_disasm_line(head, 0)}" for head in idautils.FuncItems(func.start_ea))
        path = WORK / f"analysis/sh2_{func.start_ea:X}.txt"
        path.write_text(text, encoding="utf-8")
        print("Saved", path.name)
finally:
    idapro.close_database(True)
