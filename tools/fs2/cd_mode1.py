"""MODE1/2352 sector EDC and ECC, adapted from the local FS1 work tools."""


def make_tables():
    edc,forward,backward=[],[],[0]*256
    for index in range(256):
        value=index
        for _ in range(8):
            value=(value>>1)^(0xD8018001 if value&1 else 0)
        edc.append(value)
        value=(index<<1)^(0x11D if index&128 else 0)
        forward.append(value)
        backward[index^value]=index
    return edc,forward,backward


EDC,FORWARD,BACKWARD=make_tables()


def finish_sector(original,ecc=True):
    if len(original)!=2352 or original[15]!=1:
        raise ValueError('Expected a full MODE1 sector')
    sector=bytearray(original)
    value=0
    for byte in sector[:2064]:
        value=(value>>8)^EDC[(value^byte)&255]
    sector[2064:2068]=value.to_bytes(4,'little')
    sector[2068:2076]=bytes(8)
    if not ecc:
        # The FS2 Steam data track intentionally stores zero ECC bytes.
        sector[2076:]=bytes(276)
        return bytes(sector)

    def parity(majors,minors,multiplier,increment,destination):
        size=majors*minors
        for major in range(majors):
            index=(major>>1)*multiplier+(major&1)
            first=second=0
            for _ in range(minors):
                byte=sector[12+index]
                index=(index+increment)%size
                first=FORWARD[first^byte]
                second^=byte
            first=BACKWARD[FORWARD[first]^second]
            sector[destination+major]=first
            sector[destination+major+majors]=first^second
    parity(86,24,2,86,2076)
    parity(52,43,86,88,2248)
    return bytes(sector)
