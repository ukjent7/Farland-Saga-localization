"""Patch the Steam item-description font while preserving accurate text.
All input/output artifacts stay in work. This tool does not install or access a process.
"""
from pathlib import Path
import struct
import pefile
from fls.disc.disc_decode import unpack_lz
from fls.core.path_compat import WORK
FONT_VA=0x140867cc0
FONT_LIMIT_VA=0x1408d9350

def pack_lz(data):
    flags=bytearray();payload=bytearray();pos=0;tokens=0;flag=0;flag_bits=0;size=len(data)
    while pos<size:
        maximum=min(258,size-pos);start=max(0,pos-255)
        found=data.rfind(data[pos:pos+3],start,pos+2) if maximum>=3 else -1
        count=1
        if found>=0:
            low,high=3,maximum
            while low<high:
                mid=(low+high+1)//2
                if data.rfind(data[pos:pos+mid],start,pos+mid-1)>=0:low=mid
                else:high=mid-1
            count=low;found=data.rfind(data[pos:pos+count],start,pos+count-1)
            assert 0<pos-found<=255
            payload.extend((pos-found,count-3));flag|=1<<(7-flag_bits)
        else:payload.append(data[pos])
        pos+=count;tokens+=1;flag_bits+=1
        if flag_bits==8:flags.append(flag);flag=0;flag_bits=0
    if flag_bits:flags.append(flag)
    return struct.pack('>3I',size,tokens,12+len(flags))+flags+payload

def expand_font(mono):
    result=bytearray(len(mono)*32)
    for off,value in enumerate(mono):
        for bit in range(8):result[off*32+bit*4+3]=255 if value&(128>>bit) else 0
    return bytes(result)

def font_patch(exe,mono):
    pe=pefile.PE(data=exe,fast_load=True);off=pe.get_offset_from_rva(FONT_VA-pe.OPTIONAL_HEADER.ImageBase);limit=pe.get_offset_from_rva(FONT_LIMIT_VA-pe.OPTIONAL_HEADER.ImageBase)
    original=unpack_lz(exe[off:limit]);expected=expand_font((WORK/'extracted/steam/SYSTEM/KANJI.FON').read_bytes());assert original==expected
    rgba=expand_font(mono);packed=pack_lz(rgba);assert unpack_lz(packed)==rgba
    print('Overlay font compressed bytes:',len(packed),'available:',limit-off,flush=True)
    if len(packed)>limit-off:raise ValueError('Compressed font exceeds its original region; relocation needed')
    patched=bytearray(exe);patched[off:limit]=packed+bytes(limit-off-len(packed));assert len(patched)==len(exe)
    return bytes(patched),{'va':hex(FONT_VA),'file_offset':off,'reserved_bytes':limit-off,'compressed_bytes':len(packed),'glyphs':len(mono)//32,'purpose':'Sync the independent Steam item-description RGBA font with patched Saturn KANJI.FON'}
