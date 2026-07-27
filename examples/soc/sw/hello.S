    .section .text.init
    .globl _start
_start:
    li   x1, 5
    li   x2, 7
    add  x3, x1, x2
    addi x3, x3, 1
    sll  x4, x3, x1
1:  j 1b
