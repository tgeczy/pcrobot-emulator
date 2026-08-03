"""SPEAK.COM - a tiny DOS speech server for PC-ROBOT.

Polls IN.TXT in the current directory.  When it appears, the file's contents
are handed to the resident ROBOTVOX driver (INT 2Fh AX=F001h, DS:DX -> a
length-prefixed CP852 string) and the file is deleted so the host knows the
line was taken.

Files with a special first byte are commands to the harness instead:
  '@'  quit (exit code 0)
  '#'  exit code 3 - the autoexec batch loop turns DOSBox turbo ON, then
       restarts us; the host uses this to fast-forward speech synthesis
  '!'  exit code 2 - batch turns turbo OFF and restarts us
Real payloads never collide with these: the host always prefixes text with
0xFE engine commands.

This is what lets an NVDA driver speak arbitrary text without restarting DOS.
"""
import os

from keystone import Ks, KS_ARCH_X86, KS_MODE_16

ks = Ks(KS_ARCH_X86, KS_MODE_16)
MAXLEN = 200


def build(fname, buf):
    src = """
main_loop:
    mov  ax, 0x3D00
    mov  dx, {FNAME}
    int  0x21
    jc   idle
    mov  bx, ax
    mov  ah, 0x3F
    mov  cx, {MAXLEN}
    mov  dx, {BUF}
    add  dx, 1
    int  0x21
    mov  si, ax
    push si
    mov  ah, 0x3E
    int  0x21
    mov  ah, 0x41
    mov  dx, {FNAME}
    int  0x21
    pop  si
    or   si, si
    jz   idle
    mov  di, {BUF}
    mov  al, byte ptr [di+1]
    cmp  al, 0x40
    je   done
    cmp  al, 0x23
    je   turbo_on
    cmp  al, 0x21
    je   turbo_off
    mov  ax, si
    mov  byte ptr [di], al
    mov  ax, 0xF001
    mov  dx, {BUF}
    int  0x2F
    jmp  main_loop
idle:
    mov  ah, 0x86
    xor  cx, cx
    mov  dx, 0x2710
    int  0x15
    mov  ah, 0x0B
    int  0x21
    jmp  main_loop
turbo_on:
    mov  ax, 0x4C03
    int  0x21
turbo_off:
    mov  ax, 0x4C02
    int  0x21
done:
    mov  ax, 0x4C00
    int  0x21
""".format(FNAME=fname, BUF=buf, MAXLEN=MAXLEN)
    code, _ = ks.asm(src, 0x100)
    return bytes(code)


probe = build(0x1000, 0x1100)
end = 0x100 + len(probe)
FNAME = end
BUF = FNAME + 12
code = build(FNAME, BUF)
assert len(code) == len(probe), 'layout shifted'

img = bytearray(code)
img += b'IN.TXT\x00' + b'\x00' * 5
assert 0x100 + len(img) == BUF, hex(0x100 + len(img))
img += b'\x00' * (MAXLEN + 8)

out = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'work', 'SPEAK.COM'))
open(out, 'wb').write(bytes(img))
print('wrote %s (%d bytes), IN.TXT@%04X buffer@%04X' % (out, len(img), FNAME, BUF))

from capstone import Cs, CS_ARCH_X86, CS_MODE_16
md = Cs(CS_ARCH_X86, CS_MODE_16)
for i in md.disasm(code, 0x100):
    print('  %04X  %-14s %s %s' % (i.address, i.bytes.hex(), i.mnemonic, i.op_str))
