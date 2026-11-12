// @vitest-environment jsdom
import {describe,it,expect,afterEach} from 'vitest';
import {accessibleName,queryAllWithShadow,perceptible,checkTargetFreshness,semanticElement,isOccluded} from '../src/semantic';
afterEach(()=>{document.body.innerHTML='';});
describe('semantic perception',()=>{
 it('honors name precedence and native labels',()=>{
  document.body.innerHTML='<span id="lab">Account</span><label for="email">Email</label><input id="email" aria-labelledby="lab" aria-label="Wrong" placeholder="Other">';
  const el=document.querySelector('input')!;expect(accessibleName(el)).toBe('Account');el.removeAttribute('aria-labelledby');expect(accessibleName(el)).toBe('Wrong');el.removeAttribute('aria-label');expect(accessibleName(el)).toBe('Email');
 });
 it('walks open shadow roots and same-origin frames',()=>{
  document.body.innerHTML='<div id="host"></div><iframe></iframe>';
  document.querySelector('#host')!.attachShadow({mode:'open'}).innerHTML='<button>Inside</button>';
  document.querySelector('iframe')!.contentDocument!.body.innerHTML='<button>Frame</button>';
  expect(queryAllWithShadow('button')).toHaveLength(2);
 });
 it('excludes hidden ancestors and classifies controls',()=>{
  document.body.innerHTML='<div style="opacity:0"><button>Hidden</button></div><input type="file"><div role="combobox" aria-label="City"></div>';
  expect(perceptible(document.querySelector('button')!)).toBe(false);
  expect(semanticElement(document.querySelector('input')!,'file')?.controlType).toBe('file_uploader');
  expect(semanticElement(document.querySelector('[role=combobox]')!,'city')?.controlType).toBe('custom_dropdown');
 });
 it('flags a covered target',()=>{
  document.body.innerHTML='<button>Go</button><div>Cover</div>';
  const button=document.querySelector('button')!, cover=document.querySelector('div')!;
  button.getBoundingClientRect=()=>({left:10,top:10,width:40,height:20,right:50,bottom:30,x:10,y:10,toJSON:()=>({})});
  document.elementFromPoint=()=>cover;
  expect(isOccluded(button)).toBe(true);
 });
 it('recovers unique rerenders and refuses ambiguous replacements',()=>{
  document.body.innerHTML='<button>Save</button>';
  expect(checkTargetFreshness('removed',undefined,'Save')).toBe(document.querySelector('button'));
  document.body.innerHTML='<button>Save</button><button>Save</button>';
  expect(checkTargetFreshness('removed',undefined,'Save')).toBe(null);
 });
});
