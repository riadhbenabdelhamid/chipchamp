# timer register map

_Generated from the register spec — do not edit. Bus: APB, address width: 12._

## CTRL @ 0x000
Control register

| bits | field | access | reset | description |
|---|---|---|---|---|
| 15:8 | PRESCALE | RW | 0x4 | Clock prescaler |
| 0 | EN | RW | 0x0 | Timer enable |

## COUNT @ 0x004
Live counter value

| bits | field | access | reset | description |
|---|---|---|---|---|
| 31:0 | VALUE | RO | 0x0 | Current count (hw-driven) |

## STATUS @ 0x008
Sticky status flags (write 1 to clear)

| bits | field | access | reset | description |
|---|---|---|---|---|
| 0 | OVF | W1C | 0x0 | Counter overflow |
