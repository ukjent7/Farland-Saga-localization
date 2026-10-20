"""Static analysis only; all databases and reports stay under work/."""
import idapro
import argparse
import json
from pathlib import Path
import shutil
import sys
import ida_auto
import ida_bytes
import ida_funcs
import ida_hexrays
import ida_nalt
import idautils
import idc
from fls.core.path_compat import WORK

sys.stdout.reconfigure(encoding="utf-8")
parser = argparse.ArgumentParser()
parser.add_argument("--functions", nargs="*", default=[])
parser.add_argument("--pattern", default="decrypt|cfile|cdimage|\\.cue|\\.bin|\\.ini|encrypt")
parser.add_argument("--names", default=None)
parser.add_argument("--xrefs", nargs="*", default=[])
args = parser.parse_args()
import re

analysis = WORK / "analysis"
analysis.mkdir(exist_ok=True)
source = WORK.parent / "FarlandSaga.exe"
copy = analysis / source.name
if not copy.exists():
    shutil.copy2(source, copy)
database = Path(str(copy) + ".i64")
print("Opening static IDA database", flush=True)
rc = idapro.open_database(str(database if database.exists() else copy), True)
if rc:
    raise RuntimeError(f"IDA database open failed: {rc}")
try:
    ida_auto.auto_wait()
    print("Auto analysis complete", flush=True)
    items = []
    for s in idautils.Strings():
        text = str(s)
        if re.search(args.pattern, text, re.I):
            refs = []
            for ref in idautils.XrefsTo(s.ea):
                func = ida_funcs.get_func(ref.frm)
                refs.append({"address": hex(ref.frm), "function": hex(func.start_ea) if func else None,
                             "name": idc.get_func_name(ref.frm)})
            items.append({"address": hex(s.ea), "text": text, "refs": refs})
    (analysis / "exe_strings_xrefs.json").write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    print("Saved string cross references:", len(items), flush=True)
    if args.names:
        names = []
        for ea, name in idautils.Names():
            if re.search(args.names, name, re.I):
                entry = {"address": hex(ea), "name": name,
                         "refs": [{"address": hex(x.frm), "function": hex(ida_funcs.get_func(x.frm).start_ea) if ida_funcs.get_func(x.frm) else None} for x in idautils.XrefsTo(ea)]}
                if "??_7" in name:
                    entry["vtable"] = [hex(ida_bytes.get_qword(ea + i * 8)) for i in range(22)]
                names.append(entry)
        (analysis / "exe_names.json").write_text(json.dumps(names, indent=2), encoding="utf-8")
        print(json.dumps(names, indent=2))
    for spec in args.xrefs:
        ea = int(spec, 0)
        print("XREFS", spec, [(hex(x.frm), idc.get_func_name(x.frm)) for x in idautils.XrefsTo(ea)])
    if args.functions:
        decompiler = ida_hexrays.init_hexrays_plugin()
        for spec in args.functions:
            ea = int(spec, 0)
            func = ida_funcs.get_func(ea)
            if func is None:
                print("No function", hex(ea))
                continue
            lines = [f"// {hex(func.start_ea)} {idc.get_func_name(ea)}"]
            if decompiler:
                try:
                    lines.append(str(ida_hexrays.decompile(func.start_ea)))
                except Exception as exc:
                    lines.append(f"// Decompile failed: {exc}")
            for head in idautils.FuncItems(func.start_ea):
                lines.append(f"{head:016X}  {idc.generate_disasm_line(head, 0)}")
            output = analysis / f"function_{func.start_ea:X}.txt"
            output.write_text("\n".join(lines), encoding="utf-8")
            print("Saved", output.name)
finally:
    idapro.close_database(True)
