import {test,expect} from '@playwright/test';
import {readFileSync} from 'node:fs';
const env=Object.fromEntries(readFileSync('../../.env','utf8').split(/\r?\n/).filter(l=>l&&!l.startsWith('#')).map(l=>l.split('=')));

test('forms preserve drafts and list refresh exposes loading and network recovery',async({page})=>{
  await page.goto('/');await page.getByRole('button',{name:'Sign in',exact:true}).click();
  await page.getByLabel('Username or email').fill('teacher');await page.getByLabel('Password',{exact:true}).fill(env.TEACHER_PASSWORD);await page.getByRole('button',{name:'Sign In',exact:true}).click();
  await expect(page.getByRole('button',{name:'Sign out'})).toBeVisible();
  const name=page.getByLabel('Classroom name',{exact:true});
  await page.getByRole('button',{name:'Create classroom',exact:true}).click();
  await expect(name).toHaveAttribute('aria-invalid','true');await expect(name).toBeFocused();
  await name.fill('Unsaved classroom');await expect(page.getByLabel('Current classroom')).toBeDisabled();
  await page.locator('form').filter({has:name}).getByRole('button',{name:'Clear form'}).click();
  await expect(name).toHaveValue('');await expect(page.getByLabel('Current classroom')).toBeEnabled();
  let release!:()=>void;
  const pending=new Promise<void>(resolve=>release=resolve);
  await page.route('**/api/identity/classrooms',async route=>{await pending;await route.abort('failed');});
  await page.getByRole('button',{name:'Refresh classrooms'}).click();
  await expect(page.getByRole('status').filter({hasText:'Working…'})).toBeVisible();
  release();await expect(page.getByRole('alert').filter({hasText:'The service could not be reached.'})).toBeVisible();
  await page.unroute('**/api/identity/classrooms');await page.getByRole('button',{name:'Refresh classrooms'}).click();
  await expect(page.getByRole('alert').filter({hasText:'The service could not be reached.'})).toHaveCount(0);
  await expect(page.getByRole('status').filter({hasText:'Working…'})).toHaveCount(0);
});
