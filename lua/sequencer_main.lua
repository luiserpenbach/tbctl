-- Dense T7 sequencer (spec sec 6). Enums inlined. U = single upvalue.
-- CMD 0nop 1load 2arm 3fire 4abort 5reset
-- ST 0idle 1loaded 2armed 3run 4done 5aborting 6aborted 7fault
-- ERR 0none 1hb 2host 3bad 4spark 5overrun
U.f,U.a,U.s,U.n=math.floor,bit.band,bit.rshift,bit.bnot
U.R,U.W,U.z,U.Z=MB.R,MB.W,0,1
U.S,U.E,U.N,U.M=0,0,0,0
U.T,U.V,U.X,U.Y={},{},{},{}
U.I,U.w,U.G,U.g,U.C=0xFFFF,0,0,0,0
U.t,U.i,U.o,U.h,U.j,U.H,U.p=0,0,0,0,0,U.K.h,0
U.B=U.K.e+2*U.K.s*U.K.b+U.K.r
U.b={}
function U.u(b,i) return b[i]+b[i+1]*256 end
function U.U(b,i) return b[i]+b[i+1]*256+b[i+2]*65536+b[i+3]*16777216 end
function U.P()
U.W(U.A.s,U.z,U.S)U.W(U.A.e,U.z,U.E)U.W(U.A.n,U.z,U.N*256+U.M)
U.W(U.A.c,U.z,U.C)U.W(U.A.u,U.z,U.i)U.W(U.A.m,U.Z,U.t)
end
function U.A_(v) U.p=v U.W(U.A.d,U.Z,v) end
function U.L()
-- Host allocates FIFO before writing; free it after drain so stream fits in RAM.
local n=U.R(U.A.b,U.Z)
if n>U.B then for i=1,U.f(n/2) do U.R(U.A.f,U.z) end U.W(U.A.p,U.Z,0) U.S=7 U.E=3 U.A_(U.w)U.P() return end
local k=0
for i=1,U.f(n/2) do local v=U.R(U.A.f,U.z) U.b[k+1]=U.a(v,255)U.b[k+2]=U.s(v,8)k=k+2 end
U.W(U.A.p,U.Z,0)
local ok=k>=U.K.e+U.K.r
local ns,na,ih,sf,gd,mk=0,0,0,0,0,0
if ok then
ns=U.u(U.b,1)na=U.u(U.b,3)ih=U.u(U.b,5)sf=U.u(U.b,7)gd=U.u(U.b,9)mk=U.u(U.b,11)
if ns>U.K.s or na>U.K.s or(gd~=0 and mk==0)or k~=U.K.e+(ns+na)*U.K.b+U.K.r then ok=false end
end
local off=U.K.e
if ok then
local prev=-1
for i=1,ns do
local t=U.U(U.b,off+1)local v=U.u(U.b,off+5)
if t<prev or t>3600000 then ok=false break end
prev=t U.T[i]=t U.V[i]=v off=off+U.K.b
end
end
if ok then
local prev=-1
for i=1,na do
local t=U.U(U.b,off+1)local v=U.u(U.b,off+5)
if t<prev or t>3600000 then ok=false break end
prev=t U.X[i]=t U.Y[i]=v off=off+U.K.b
end
end
if ok then local c=U.crc16(U.b,off) if c~=U.u(U.b,off+1) then ok=false else U.C=c end end
if ok then
U.N,U.M,U.I,U.w,U.G,U.g=ns,na,ih,sf,gd,mk U.i,U.t=0,0
-- DIO_INHIBIT must stay float32-exact: avoid 0xFFFF0000+U.I (rounds to 0xFFFFFFFF).
-- Lower-16 inhibit is enough for FIO; unused EIO/CIO stay direction=input.
U.W(U.A.i,U.Z,U.I)U.W(U.A.o,U.Z,U.a(U.n(U.I),65535))U.S=1 U.E=0
else U.S=7 U.E=3 U.A_(U.w) end
U.P()
end
function U.Q(r)
U.S=5 U.E=r U.t=0 U.i=1 U.o=0
if U.M==0 then U.A_(U.w)U.S=6 end
U.P()
end
function U.C_()
local c=U.R(U.A.q,U.z)
if c==0 then return end
U.W(U.A.q,U.z,0)
if c==1 and U.S==0 then U.L()
elseif c==2 and U.S==1 then
if U.R(U.A.a,U.z)==U.K.m then
U.W(U.A.a,U.z,0)
local t=U.R(U.A.t,U.z)U.H=(t>0)and t or U.K.h
U.A_(U.w)U.h=U.R(U.A.h,U.z)U.j=0 U.S=2 U.P()
end
elseif c==3 and U.S==2 then U.S=3 U.t=0 U.i=1 U.o=0 U.P()
elseif c==4 then
if U.S==3 then U.Q(2) elseif U.S==2 then U.A_(U.w)U.S=6 U.E=2 U.P() end
elseif c==5 and(U.S==1 or U.S==4 or U.S==6 or U.S==7) then
if U.N>0 then U.A_(U.w) end
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
if U.G>0 then
if U.a(U.p,U.g)~=0 then U.o=U.o+1 if U.o>U.G then U.A_(U.a(U.p,U.n(U.g)))U.Q(4) return end
else U.o=0 end
end
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
if U.i>U.M then U.A_(U.w)U.S=6 U.P() return end
U.W(U.A.u,U.z,U.i)
if U.a(U.t,15)==0 then U.W(U.A.m,U.Z,U.t) end
end
end
U.W(U.A.v,U.z,U.K.v)U.P()
LJ.IntervalConfig(0,U.K.k)
while true do
local x=LJ.CheckInterval(0)
if x and x>0 then
if x>250 and(U.S==3 or U.S==5) then U.Q(5) end
if x>1000 then x=1000 end
for i=1,x do U.K_() end
end
end
