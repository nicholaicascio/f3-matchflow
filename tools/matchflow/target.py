"""Read-only local target inspection. Never returns raw instruction bytes.

Called only by the trusted runner with a broker-bound address and extent. Output
is private worker context, never a service payload or a repository fixture.
"""
import struct
from pathlib import Path


def inspect(image, address, size, offset=0, limit=200):
    import capstone
    if type(address) is not int or type(size) is not int or not 0 < size <= 2 * 1024 * 1024:
        raise ValueError('invalid assigned extent')
    if type(offset) is not int or not 0 <= offset < size or type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError('invalid target page')
    data = Path(image).read_bytes()
    if data[:2] != b'MZ':
        raise ValueError('not a PE image')
    pe = struct.unpack_from('<I', data, 0x3c)[0]
    if data[pe:pe+4] != b'PE\0\0' or struct.unpack_from('<H', data, pe+24)[0] != 0x10b:
        raise ValueError('not a PE32 image')
    sections = struct.unpack_from('<H', data, pe+6)[0]
    optional_size = struct.unpack_from('<H', data, pe+20)[0]
    base = struct.unpack_from('<I', data, pe+24+28)[0]
    headers = pe+24+optional_size
    code = None
    for i in range(sections):
        virtual_size, rva, raw_size, raw_offset = struct.unpack_from('<IIII', data, headers+i*40+8)
        delta = address - (base+rva)
        if 0 <= delta and delta+size <= raw_size:
            code = data[raw_offset+delta:raw_offset+delta+size]
            break
    if code is None or len(code) != size:
        raise ValueError('assigned extent is outside the image')
    decoder = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32)
    rows = []
    next_offset = None
    for instruction in decoder.disasm(code, address):
        at = instruction.address-address
        if at < offset:
            continue
        if len(rows) >= limit:
            next_offset = at
            break
        rows.append(dict(offset='0x%x' % at, instruction=instruction.mnemonic, operands=instruction.op_str))
    return dict(address='0x%08x' % address, size=size, instructions=rows,
                next_offset=next_offset, privacy='local only; never paste disassembly into service notes')
