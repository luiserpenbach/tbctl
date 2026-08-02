-- Dense T7 sequencer (spec sec 6). Enums inlined. U = single upvalue.
-- CMD 0nop 1load 2arm 3fire 4abort 5reset
-- ST 0idle 1loaded 2armed 3run 4done 5aborting 6aborted 7fault
-- ERR 0none 1hb 2host 3bad 4spark 5overrun
-- bit.bnot is BANNED in this file (bug-hunt H7): T7 Lua numbers are float32.
-- If the firmware's bnot returns unsigned 32-bit values (e.g. bnot(0xFF80) =
-- 0xFFFF007F = 4294901887) the result exceeds float32's 2^24 exact-integer
-- range and ROUNDS before a following band() can mask it back down. 16-bit
-- complements are therefore computed with exact subtraction instead:
--   full complement:  65535 - band(x,65535)
--   clear mask bits:  p - band(p,mask)
-- Every intermediate stays <= 65535, which is float32-exact under EITHER
-- firmware bnot signedness convention (signed int32 or unsigned u32).
U.f,U.a,U.s=math.floor,bit.band,bit.rshift
U.R,U.W,U.z,U.Z=MB.R,MB.W,0,1
U.S,U.E,U.N,U.M=0,0,0,0
U.T,U.V,U.X,U.Y={},{},{},{}
U.I,U.w,U.G,U.g,U.C=0xFFFF,0,0,0,0
U.t,U.i,U.o,U.h,U.j,U.H,U.p=0,0,0,0,0,U.K.h,0
-- U.D=1 once a successful LOAD has programmed DIO_INHIBIT/DIO_DIRECTION (C3).
-- Until then DIO_STATE is NEVER written: the power-on default (all lines
-- inputs, inhibit 0) means a state write would drive every DIO as output.
-- U.B=1 when an abort just started: the main loop drops the rest of the
-- current catch-up batch so abort steps pace at real 1 ms ticks, not at
-- Modbus-write speed (H6).
U.D,U.B,U.c_=0,0,0
function U.P()
U.W(U.A.s,U.z,U.S)U.W(U.A.e,U.z,U.E)U.W(U.A.n,U.z,U.N*256+U.M)
U.W(U.A.c,U.z,U.C)U.W(U.A.u,U.z,U.i)U.W(U.A.m,U.Z,U.t)
end
function U.A_(v) U.p=v U.W(U.A.d,U.Z,v) end
-- CRC-16/CCITT-FALSE, one byte into running U.c_ (table-free, load-time only)
function U.cb(b)
local c=bit.bxor(U.c_,b*256)
for i=1,8 do if U.a(c,32768)~=0 then c=U.a(bit.bxor(c*2,4129),65535) else c=U.a(c*2,65535) end end
U.c_=c
end
-- pop one FIFO word, feeding both bytes (LE order) into the running CRC
function U.rw()
local v=U.R(U.A.f,U.z)
U.cb(U.a(v,255))U.cb(U.s(v,8))
return v
end
-- drain FIFO residue and free the allocation so the stream can use the RAM
function U.F_()
for i=1,U.f(U.R(U.A.b,U.Z)/2) do U.R(U.A.f,U.z) end
U.W(U.A.p,U.Z,0)
end
-- D9 spark max-on guard on the COMMANDED word; forces the spark bit(s) low
-- (exact subtraction, see bnot note above) and returns true when it trips.
function U.S_()
if U.G==0 then return false end
if U.a(U.p,U.g)~=0 then
U.o=U.o+1
if U.o>U.G then U.A_(U.p-U.a(U.p,U.g))return true end
else U.o=0 end
return false
end
function U.L()
-- Streaming word-parse: the sec-5 wire format is u16-aligned throughout, so
-- no byte buffer is needed. A 1548-entry Lua byte table OOMed the T7 heap
-- with a 100-step table; this reads the FIFO word-by-word with an
-- incremental CRC instead. Host allocates FIFO before upload; freed below.
local nw=U.f(U.R(U.A.b,U.Z)/2)
local ok=(nw>=7 and nw<=7+2*U.K.s*3)
local ns,na,ih,sf,gd,mk=0,0,0,0,0,0
U.c_=0xFFFF
if ok then
ns=U.rw()na=U.rw()ih=U.rw()sf=U.rw()gd=U.rw()mk=U.rw()
if ns>U.K.s or na>U.K.s or(gd~=0 and mk==0)or nw~=7+(ns+na)*3 then ok=false end
end
if ok then
local prev=-1
for i=1,ns do
local l=U.rw()local hh=U.rw()local v=U.rw()
local t=l+hh*65536
if t<prev or t>3600000 then ok=false break end
prev=t U.T[i]=t U.V[i]=v
end
end
if ok then
local prev=-1
for i=1,na do
local l=U.rw()local hh=U.rw()local v=U.rw()
local t=l+hh*65536
if t<prev or t>3600000 then ok=false break end
prev=t U.X[i]=t U.Y[i]=v
end
end
if ok then
local cc=U.c_
if cc~=U.R(U.A.f,U.z) then ok=false else U.C=cc end
end
-- drain residue (bad tables / early exit) and free the FIFO for the stream
U.F_()
if ok then
U.N,U.M,U.I,U.w,U.G,U.g=ns,na,ih,sf,gd,mk U.i,U.t=0,0
-- DIO_INHIBIT must stay float32-exact: avoid 0xFFFF0000+U.I (rounds to 0xFFFFFFFF).
-- Lower-16 inhibit is enough for FIO; unused EIO/CIO stay direction=input.
U.W(U.A.i,U.Z,U.I)U.W(U.A.o,U.Z,65535-U.a(U.I,65535))U.D=1 U.S=1 U.E=0
else
-- FAULT (C3): publish honest zeros, not the previous table's CRC/counts,
-- and touch DIO_STATE only if a previous LOAD configured inhibit/direction.
-- On a first-boot FAULT the lines stay at their power-on input default.
U.N,U.M,U.C=0,0,0
U.S=7 U.E=3
if U.D==1 then U.A_(U.w) end
end
U.P()
end
function U.Q(r)
-- start the staged abort; NEVER restart one already in progress (H6): that
-- would replay early abort words out of context and clobber the original
-- error code (the abort reason is post-incident evidence).
if U.S==5 then return end
U.S=5 U.E=r U.t=0 U.i=1 U.o=0 U.B=1
if U.M==0 then U.A_(U.w)U.S=6 end
U.P()
end
function U.C_()
local c=U.R(U.A.q,U.z)
if c==0 then return end
U.W(U.A.q,U.z,0)
if c==1 then
-- LOAD outside IDLE is refused, but still drains+frees the FIFO (L5):
-- leaving 2048 B allocated+full starves the T7 shared heap.
if U.S==0 then U.L() else U.F_() end
elseif c==2 and U.S==1 then
if U.R(U.A.a,U.z)==U.K.m then
U.W(U.A.a,U.z,0)
local t=U.R(U.A.t,U.z)U.H=(t>0)and t or U.K.h
U.A_(U.w)U.h=U.R(U.A.h,U.z)U.j=0 U.S=2 U.P()
end
elseif c==3 and U.S==2 then U.S=3 U.t=0 U.i=1 U.o=0 U.P()
elseif c==4 then
if U.S==3 then U.Q(2)
-- ABORT from ARMED or DONE (M11): drive the safe word, land in ABORTED
elseif U.S==2 or U.S==4 then U.A_(U.w)U.S=6 U.E=2 U.B=1 U.P()
-- ABORT in FAULT: re-assert the safe word if DIO was ever configured (C3),
-- but stay FAULT and keep the fault's error code
elseif U.S==7 and U.D==1 then U.A_(U.w) end
elseif c==5 and(U.S==1 or U.S==2 or U.S==4 or U.S==6 or U.S==7) then
-- RESET; from ARMED (M6) this is the clean stand-down path. Safe word is
-- gated on U.D: a first-boot FAULT reset must not touch unconfigured DIO.
if U.D==1 then U.A_(U.w) end
U.S=0 U.E=0 U.i=0 U.t=0 U.P()
end
end
function U.K_()
U.C_()
if U.S==2 then
local hb=U.R(U.A.h,U.z)
if hb~=U.h then U.h=hb U.j=0 else U.j=U.j+1 end
if U.j>U.H then U.Q(1) end
return
end
if U.S==3 then
U.t=U.t+1
while U.i<=U.N and U.T[U.i]<=U.t do U.A_(U.V[U.i])U.i=U.i+1 end
if U.S_() then U.Q(4) return end
local hb=U.R(U.A.h,U.z)
if hb~=U.h then U.h=hb U.j=0 else U.j=U.j+1 end
if U.j>U.H then U.Q(1) return end
if U.i>U.N then U.S=4 U.P() return end
U.W(U.A.u,U.z,U.i)
if U.a(U.t,15)==0 then U.W(U.A.m,U.Z,U.t) end
return
end
if U.S==5 then
U.t=U.t+1
while U.i<=U.M and U.X[U.i]<=U.t do U.A_(U.Y[U.i])U.i=U.i+1 end
-- spark guard stays live during ABORTING on the commanded word (L7); a
-- trip forces the bit low but neither restarts the abort nor replaces
-- the original error code.
U.S_()
if U.i>U.M then U.A_(U.w)U.S=6 U.P() return end
U.W(U.A.u,U.z,U.i)
if U.a(U.t,15)==0 then U.W(U.A.m,U.Z,U.t) end
end
end
U.W(U.A.v,U.z,U.K.v)U.P()
-- Every ms-vs-tick comparison in this file assumes 1 tick == 1 ms; codegen
-- hard-fails if constants.tick_ms ~= 1 (F4), so U.K.k==1 here always.
LJ.IntervalConfig(0,U.K.k)
-- ASSUMED, pending one-time hardware verification (bug-hunt F5, pairs with
-- H7): LJ.CheckInterval(0) returns the COUNT of elapsed tick_ms intervals
-- since the last poll (nil/0 when none elapsed). If the deployed firmware
-- instead returns a boolean/1 per call, overrun detection (x>250), the
-- x>1000 clamp and catch-up are all inert and the timeline silently dilates
-- under load. Measure once on hardware before trusting the timing design.
while true do
local x=LJ.CheckInterval(0)
if x and x>0 then
-- overrun abort fires from RUNNING only (H6): a >=250 ms stall while
-- already ABORTING must not restart the abort or overwrite its error code
if x>250 and U.S==3 then U.Q(5) end
if x>1000 then
-- clamp tick processing, but never lose elapsed time from the ARMED
-- heartbeat age (L6): staleness detection must account the full gap
if U.S==2 then U.j=U.j+(x-1000) end
x=1000
end
-- U.B: an abort begun mid-batch drops the remaining backlog ticks so the
-- abort table executes at real-time pacing instead of collapsed (H6)
for i=1,x do U.K_() if U.B==1 then U.B=0 break end end
end
end
