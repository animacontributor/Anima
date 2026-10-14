'use strict';
const test=require('node:test'),assert=require('node:assert/strict');
const {clean,position}=require('../android/assets/phone_spatial.js');
test('positions use head-relative forward, left/right and vertical coordinates',()=>{
  const front=position({azimuth:0,elevation:0,distance:1});assert.ok(Math.abs(front.x)<1e-10);assert.equal(front.z,-1);
  assert.ok(position({azimuth:-90,elevation:0,distance:.2}).x<0);
  assert.ok(position({azimuth:90,elevation:0,distance:.2}).x>0);
  assert.ok(position({azimuth:0,elevation:45,distance:1}).y>0);
  assert.ok(position({azimuth:180,elevation:0,distance:1}).z>0);
});
test('distance is preserved by the full three dimensional position',()=>{
  const p=position({azimuth:42,elevation:-35,distance:1.7});assert.ok(Math.abs(Math.hypot(p.x,p.y,p.z)-1.7)<1e-10);
});
test('invalid saved settings cannot put nonfinite values into audio parameters',()=>{
  const s=clean({enabled:true,azimuth:'bad',elevation:Infinity,distance:-10});
  assert.equal(s.enabled,true);assert.equal(s.azimuth,0);assert.equal(s.elevation,0);assert.equal(s.distance,.11);
});
