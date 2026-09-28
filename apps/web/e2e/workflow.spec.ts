import {test,expect,Page} from '@playwright/test';
import {readFileSync} from 'node:fs';
const env=Object.fromEntries(readFileSync('../../.env','utf8').split(/\r?\n/).filter(l=>l&&!l.startsWith('#')).map(l=>l.split('=')));
async function login(page:Page,user:string,password:string){
  await page.goto('/');await page.getByRole('button',{name:'Sign in',exact:true}).click();
  await page.getByLabel('Username or email').fill(user);await page.getByLabel('Password',{exact:true}).fill(password);await page.getByRole('button',{name:'Sign In',exact:true}).click();
  await expect(page.getByRole('button',{name:'Sign out'})).toBeVisible();
}
test('teacher and student complete a real assessment through Kong',async({browser})=>{
  const teacher=await browser.newPage(),student=await browser.newPage();
  await login(student,'student',env.STUDENT_PASSWORD);
  const studentId=await student.locator('.identity-note code').innerText();
  await login(teacher,'teacher',env.TEACHER_PASSWORD);
  const suffix=Date.now().toString();
  await teacher.getByLabel('Classroom name',{exact:true}).fill('Browser '+suffix);await teacher.getByRole('button',{name:'Create classroom',exact:true}).click();
  await expect(teacher.getByText('Classroom created.',{exact:true})).toBeVisible();
  await teacher.getByLabel('Current classroom').selectOption({label:'Browser '+suffix});
  await teacher.getByLabel('Student ID',{exact:true}).filter({visible:true}).fill(studentId);await teacher.getByRole('button',{name:'Enroll student',exact:true}).click();await expect(teacher.getByText('Student enrolled.',{exact:true})).toBeVisible();
  await teacher.getByRole('button',{name:'Curriculum',exact:true}).click();await teacher.getByLabel('Name',{exact:true}).fill('Browser skill '+suffix);await teacher.getByRole('button',{name:'Add curriculum',exact:true}).click();await expect(teacher.getByText('Curriculum added.',{exact:true})).toBeVisible();
  await teacher.getByRole('button',{name:'Assessments',exact:true}).click();await teacher.getByRole('button',{name:'Refresh assessments'}).click();
  await teacher.getByLabel('Assessment title').fill('Browser assessment '+suffix);await teacher.getByLabel('Question',{exact:true}).fill('What is 0.1 plus 0.2?');await teacher.getByLabel('Curriculum skill').selectOption({label:'Browser skill '+suffix});await teacher.getByLabel('Correct answer').fill('0.3');await teacher.getByRole('button',{name:'Create assessment',exact:true}).click();await expect(teacher.getByText('Assessment created.',{exact:true})).toBeVisible();
  await student.getByRole('button',{name:'Refresh classrooms'}).click();await student.getByLabel('Current classroom').selectOption({label:'Browser '+suffix});await student.getByRole('button',{name:'Assessments',exact:true}).click();
  await student.getByRole('button',{name:'Open assessment',exact:true}).click();await student.getByLabel('What is 0.1 plus 0.2?').fill('0.30');await student.getByRole('button',{name:'Submit answers',exact:true}).click();await expect(student.getByText('Answers submitted. Your teacher will review the grade.',{exact:true})).toBeVisible();
  await teacher.getByRole('button',{name:'Grading review',exact:true}).click();await expect(async()=>{await teacher.getByRole('button',{name:'Refresh grades'}).click();await expect(teacher.getByRole('button',{name:'Review grade',exact:true})).toBeVisible();}).toPass({timeout:30000});
  await teacher.getByRole('button',{name:'Review grade',exact:true}).click();await teacher.getByLabel('Review notes').fill('Reviewed exact decimal answer.');await teacher.getByRole('button',{name:'Finalize grade',exact:true}).click();await expect(teacher.getByText('Grade finalized. Performance will update after event processing.',{exact:true})).toBeVisible();
  await student.getByRole('button',{name:'Learning performance',exact:true}).click();await expect(async()=>{await student.getByRole('button',{name:'View performance'}).click();await expect(student.getByRole('cell',{name:'100%'})).toBeVisible();}).toPass({timeout:30000});
  await expect(student.getByRole('cell',{name:'Insufficient evidence'})).toBeVisible();await student.screenshot({path:'test-results/performance-desktop.png',fullPage:true});
  await student.setViewportSize({width:390,height:844});await student.screenshot({path:'test-results/performance-mobile.png',fullPage:true});
  expect(await student.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth)).toBe(true);
  await teacher.close();await student.close();
});

test('anonymous workspace has honest unavailable state and keyboard navigation',async({page})=>{
  await page.goto('/');await expect(page.getByRole('heading',{level:1})).toBeVisible();await expect(page.getByText(/AI questions, feedback and document chat are not implemented/)).toBeVisible();
  await page.keyboard.press('Tab');await expect(page.getByRole('link',{name:'Skip to workspace'})).toBeFocused();
  await page.screenshot({path:'test-results/welcome.png',fullPage:true});
});
