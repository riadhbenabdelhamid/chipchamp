---
name: efpga-spin
description: Author a brand-new user design from a one-line request and prove
  it on the FABulous eFPGA - fabric, bitstream, gates, report.done. Use when
  asked to put a small design onto the eFPGA / spin a design onto the fabric.
---
# Spin a requested design onto the eFPGA

The user's message names WHAT to build. Everything else is fixed rails:
the FABulous project lives at `efpga-fabulous/`, and the deliverable is a
routed bitstream for a design that did not exist a minute ago, closed
properly through `report.done`.

## The rails

1. **Generate the fabric** (`efpga-fabulous.fabric`). ~6 s; 55 HDL files.
   Skip only if a bitstream job in THIS session already proved the fabric.

2. **Author the design.** Keep it small — a few hundred flops at most, or it
   will not route on this fabric. It must use EXACTLY this port interface
   (same as `efpga-fabulous/user_design/sequential_16bit_en.v` — read it
   first if unsure):

       module <yours> (
           input  wire        clk,
           input  wire [27:0] io_in,
           output wire [27:0] io_out,
           output wire [27:0] io_oeb
       );

   Drive every bit of `io_out` and `io_oeb` (`io_oeb = 28'd0` for outputs).
   Save as `efpga-fabulous/user_design/<name>.v`.

3. **Retarget the pad ring.** `efpga-fabulous/user_design/top_wrapper.v`
   instantiates the CURRENT user design by module name as `<module> top_i (`.
   Replace that module name with yours (fs.edit, one line). Do not touch
   anything else in the wrapper — `Global_Clock` there is a FABulous flow
   primitive; its missing definition is normal.

4. **Bitstream** (`efpga-fabulous.bitstream`) with the design path relative
   to the project: `user_design/<name>.v` — the FILE, never a bare module
   name, never the wrapper. On a build failure, read the job's fail reason,
   fix YOUR design, and re-run.

5. **Close on evidence.** Report bitstream bytes, `routed`, and fmax from
   the passing job, then `report.done`. If it rejects, the gate ladder names
   what is still owed — pay it (usually: re-prove after your last edit),
   then claim again. Never claim without a passing bitstream job newer than
   your last edit.
