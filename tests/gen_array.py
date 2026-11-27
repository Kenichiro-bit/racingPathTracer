#!/usr/bin/env python3
"""Generate a synthetic SRAM array netlist (CDL) for racepath tests.

ROWS x COLS cells. Each word line runs through a series resistor per column
(post-layout style), and each column has its own precharge, sense amp and
output latch. Instances use both the ``X ... / SUB`` and ``$PINS`` forms.

Expected result: 6 race groups (one per race of the demo column), each with
group_size == COLS.

usage: python gen_array.py ROWS COLS OUT.cdl
"""
import sys

CELLS = """\
.GLOBAL VDD VSS
.SUBCKT INV A Y VDD VSS
*.PININFO A:I Y:O VDD:B VSS:B
MP0 Y A VDD VDD pch W=0.4u L=0.04u
MN0 Y A VSS VSS nch W=0.2u L=0.04u
.ENDS
.SUBCKT SRAM6T BL BLB WL VDD VSS
MPU0 Q QB VDD VDD pch W=0.1u L=0.04u
MPD0 Q QB VSS VSS nch W=0.2u L=0.04u
MPU1 QB Q VDD VDD pch W=0.1u L=0.04u
MPD1 QB Q VSS VSS nch W=0.2u L=0.04u
MPG0 BL WL Q VSS nch W=0.15u L=0.04u
MPG1 BLB WL QB VSS nch W=0.15u L=0.04u
.ENDS
.SUBCKT SA BL BLB SAE SAT SAC VDD VSS
MCP0 BL SAE SAT VDD pch W=0.3u L=0.04u
MCP1 BLB SAE SAC VDD pch W=0.3u L=0.04u
MP1 SAT SAC VDD VDD pch W=0.3u L=0.04u
MP2 SAC SAT VDD VDD pch W=0.3u L=0.04u
MN1 SAT SAC TAIL VSS nch W=0.6u L=0.04u
MN2 SAC SAT TAIL VSS nch W=0.6u L=0.04u
MNT TAIL SAE VSS VSS nch W=1u L=0.04u
.ENDS
.SUBCKT LATCH DIN CK CKB Q VDD VSS
XDRV DIN DL VDD VSS / INV
MTN DL CKB X VSS nch W=0.2u L=0.04u
MTP DL CK X VDD pch W=0.4u L=0.04u
XQ X Q VDD VSS / INV
MFP1 F1 Q VDD VDD pch W=0.2u L=0.04u
MFP2 X CKB F1 VDD pch W=0.2u L=0.04u
MFN2 X CK F2 VSS nch W=0.1u L=0.04u
MFN1 F2 Q VSS VSS nch W=0.1u L=0.04u
.ENDS
"""


def gen(rows, cols):
    out = ["* racepath synthetic array %d rows x %d cols" % (rows, cols), CELLS]
    # one column: precharge, cells, SA and latch
    col_wl = " ".join("WL<%d>" % r for r in range(rows))
    out.append(".SUBCKT COL %s PRE_N SAE CKI CKBI Q VDD VSS" % col_wl)
    out.append("MPRE0 BL PRE_N VDD VDD pch W=0.5u L=0.04u")
    out.append("MPRE1 BLB PRE_N VDD VDD pch W=0.5u L=0.04u")
    out.append("MEQ BL PRE_N BLB VDD pch W=0.3u L=0.04u")
    for r in range(rows):
        if r % 2:
            out.append("XC%d SRAM6T $PINS BL=BL BLB=BLB WL=WL<%d> VDD=VDD VSS=VSS" % (r, r))
        else:
            out.append("XC%d BL BLB WL<%d> VDD VSS / SRAM6T" % (r, r))
    out.append("XSA BL BLB SAE SAT SAC VDD VSS / SA")
    out.append("XLAT SAT CKBI CKI Q VDD VSS / LATCH")
    out.append(".ENDS")

    qs = " ".join("Q%d" % c for c in range(cols))
    out.append(".SUBCKT ARRAY CLK %s VDD VSS" % qs)
    out.append("*.PININFO CLK:I %s VDD:B VSS:B" % " ".join("Q%d:O" % c for c in range(cols)))
    out.append("XI0 CLK CLKB VDD VSS / INV")
    out.append("XI1 CLKB CLKBUF VDD VSS / INV")
    for r in range(rows):
        out.append("XWB%d CLKBUF WLB<%d> VDD VSS / INV" % (r, r))
        out.append("XWD%d WLB<%d> WL<%d>_0 VDD VSS / INV" % (r, r, r))
        # word line wire: one series resistor per column segment
        for c in range(cols):
            out.append("RWL%d_%d WL<%d>_%d WL<%d>_%d 10" % (r, c, r, c, r, c + 1))
    out.append("XP0 CLKBUF PREB VDD VSS / INV")
    out.append("XP1 PREB PRE_N VDD VSS / INV")
    prev = "CLKBUF"
    for i in range(1, 7):
        nxt = "SAE" if i == 6 else "S%d" % i
        out.append("XS%d %s %s VDD VSS / INV" % (i - 1, prev, nxt))
        prev = nxt
    out.append("XL0 CLKBUF CKI VDD VSS / INV")
    out.append("XL1 CKI CKBI VDD VSS / INV")
    for c in range(cols):
        wls = " ".join("WL<%d>_%d" % (r, c + 1) for r in range(rows))
        out.append("XCOL%d %s PRE_N SAE CKI CKBI Q%d VDD VSS / COL" % (c, wls, c))
    out.append(".ENDS")
    return "\n".join(out) + "\n"


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 3:
        sys.exit(__doc__)
    rows, cols, path = int(argv[0]), int(argv[1]), argv[2]
    with open(path, "w") as f:
        f.write(gen(rows, cols))


if __name__ == "__main__":
    main()
