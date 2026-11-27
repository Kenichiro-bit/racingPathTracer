#!/usr/bin/env python3
"""Generate random but well-formed transistor netlists for the Python/JS parity test.

Each netlist mixes the structures racepath classifies (inverter chains, latches,
sense amps, precharge, pass gates, clocked gates, buses, series resistors) wired at
random, in CDL or Spectre syntax.

usage: python gen_random.py SEED OUT.(cdl|scs)
"""
import random
import sys

CELLS_CDL = """\
.GLOBAL VDD VSS
.SUBCKT INV A Y VDD VSS
MP0 Y A VDD VDD pch W=0.4u L=0.04u
MN0 Y A VSS VSS nch W=0.2u L=0.04u
.ENDS
.SUBCKT NAND2 A B Y VDD VSS
MP0 Y A VDD VDD pch W=0.4u L=0.04u
MP1 Y B VDD VDD pch W=0.4u L=0.04u
MN0 Y A X VSS nch W=0.4u L=0.04u
MN1 X B VSS VSS nch W=0.4u L=0.04u
.ENDS
.SUBCKT TG D CK CKB Q VDD VSS
MN0 D CK Q VSS nch W=0.2u L=0.04u
MP0 D CKB Q VDD pch W=0.4u L=0.04u
.ENDS
.SUBCKT LATCH DIN CK CKB Q VDD VSS
XDRV DIN DL VDD VSS INV
MTN DL CKB X VSS nch W=0.2u L=0.04u
MTP DL CK X VDD pch W=0.4u L=0.04u
XQ X Q VDD VSS INV
MFP1 F1 Q VDD VDD pch W=0.2u L=0.04u
MFP2 X CKB F1 VDD pch W=0.2u L=0.04u
MFN2 X CK F2 VSS nch W=0.1u L=0.04u
MFN1 F2 Q VSS VSS nch W=0.1u L=0.04u
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
.SUBCKT CELL BL BLB WL VDD VSS
MPU0 Q QB VDD VDD pch W=0.1u L=0.04u
MPD0 Q QB VSS VSS nch W=0.2u L=0.04u
MPU1 QB Q VDD VDD pch W=0.1u L=0.04u
MPD1 QB Q VSS VSS nch W=0.2u L=0.04u
MPG0 BL WL Q VSS nch W=0.15u L=0.04u
MPG1 BLB WL QB VSS nch W=0.15u L=0.04u
.ENDS
"""


def gen(seed):
    rnd = random.Random(seed)
    nets = ["CLK", "EN", "D0", "D1"]
    lines = []
    k = 0

    def new_net(base="N"):
        nonlocal k
        k += 1
        n = f"{base}{k}"
        nets.append(n)
        return n

    def pick():
        # prefer recent nets so that paths get long and reconverge
        return nets[-1 - min(int(rnd.expovariate(0.25)), len(nets) - 1)]

    for i in range(rnd.randint(15, 45)):
        kind = rnd.choice(["inv", "inv", "inv", "nand", "tg", "latch", "sa", "pre", "cell", "res", "bus", "mos"])
        if kind == "inv":
            lines.append(f"XI{i} {pick()} {new_net()} VDD VSS INV")
        elif kind == "nand":
            lines.append(f"XN{i} {pick()} {pick()} {new_net()} VDD VSS NAND2")
        elif kind == "tg":
            ck = pick()
            ckb = new_net("CB")
            lines.append(f"XTB{i} {ck} {ckb} VDD VSS INV")
            lines.append(f"XT{i} {pick()} {ck} {ckb} {new_net()} VDD VSS TG")
        elif kind == "latch":
            ck = pick()
            ckb = new_net("LB")
            lines.append(f"XLB{i} {ck} {ckb} VDD VSS INV")
            lines.append(f"XL{i} {pick()} {ckb} {ck} {new_net('Q')} VDD VSS LATCH")
        elif kind == "sa":
            bl, blb = new_net("BL"), new_net("BLB")
            wl = pick()
            lines.append(f"XC{i} {bl} {blb} {wl} VDD VSS CELL")
            lines.append(f"MPR{i}a {bl} {pick()} VDD VDD pch W=0.5u L=0.04u")
            lines.append(f"MPR{i}b {blb} {pick()} VDD VDD pch W=0.5u L=0.04u")
            lines.append(f"XS{i} {bl} {blb} {pick()} {new_net('SAT')} {new_net('SAC')} VDD VSS SA")
        elif kind == "pre":
            bl = new_net("PB")
            lines.append(f"MPP{i} {bl} {pick()} VDD VDD pch W=0.5u L=0.04u")
            lines.append(f"MPN{i} {bl} {pick()} {pick()} VSS nch W=0.2u L=0.04u")
        elif kind == "cell":
            lines.append(f"XCC{i} {pick()} {pick()} {pick()} VDD VSS CELL")
        elif kind == "res":
            a = pick()
            lines.append(f"R{i} {a} {new_net('RW')} 10")
        elif kind == "bus":
            src = pick()
            for b in range(rnd.randint(2, 4)):
                lines.append(f"XB{i}_{b} {src} WL{i}<{b}> VDD VSS INV")
            nets.append(f"WL{i}<0>")
        else:
            ty = rnd.choice(["pch", "nch"])
            lines.append(f"MX{i} {pick()} {pick()} {rnd.choice([pick(), 'VDD', 'VSS'])} VSS {ty} W=0.2u L=0.04u")
    ports = ["CLK", "EN", "D0", "D1"]
    outs = [n for n in nets[-3:] if "<" not in n]
    head = [".SUBCKT TOP " + " ".join(ports + outs) + " VDD VSS",
            "*.PININFO " + " ".join(f"{p}:I" for p in ports) + " " + " ".join(f"{o}:O" for o in outs)
            + " VDD:B VSS:B"]
    return CELLS_CDL + "\n".join(head + lines + [".ENDS"]) + "\n"


def to_spectre(cdl):
    out = ["// random racepath netlist (Spectre)", "simulator lang=spectre"]
    for ln in cdl.splitlines():
        t = ln.split()
        if not t or t[0].startswith("*"):
            continue
        k = t[0].upper()
        if k == ".GLOBAL":
            out.append("global " + " ".join(t[1:]))
        elif k == ".SUBCKT":
            out.append(f"subckt {t[1]} ({' '.join(t[2:])})")
        elif k == ".ENDS":
            out.append("ends")
        elif k[0] == "M":
            out.append(f"{t[0]} ({' '.join(t[1:5])}) {t[5]} " + " ".join(p.lower() for p in t[6:]))
        elif k[0] == "X":
            out.append(f"{t[0]} ({' '.join(t[1:-1])}) {t[-1]}")
        elif k[0] == "R":
            out.append(f"{t[0]} ({t[1]} {t[2]}) resistor r={t[3]}")
    return "\n".join(out) + "\n"


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    seed, path = int(argv[0]), argv[1]
    text = gen(seed)
    if path.endswith(".scs"):
        text = to_spectre(text)
    with open(path, "w") as f:
        f.write(text)


if __name__ == "__main__":
    main()
