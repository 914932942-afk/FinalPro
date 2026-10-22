/* Browser acceptance check against an isolated demo workbench. Never use production credentials. */
const fs = require('fs');
const path = require('path');
const assert = require('assert/strict');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');

async function main() {
  const [base, fixtureDir, outputDir] = process.argv.slice(2);
  if (!base || !fixtureDir || !outputDir) throw new Error('Usage: node script URL isolated-data-dir output-dir');
  const keys = JSON.parse(fs.readFileSync(path.join(fixtureDir, 'auth.keys.json'), 'utf8'));
  const accountText=fs.readFileSync(path.join(fixtureDir,'auth.accounts.txt'),'utf8');
  const passwords=Object.fromEntries([...accountText.matchAll(/^账号：([^\r\n]+)\r?\n身份：[^\r\n]+\r?\n初始密码：([^\r\n]+)/gm)].map(match=>[match[1],match[2]]));
  fs.mkdirSync(outputDir, {recursive:true});
  const browser = await chromium.launch({headless:true});
  const page = await browser.newPage({viewport:{width:1440,height:1000}});
  page.setDefaultTimeout(15000);
  const checks = {}, errors = [];
  page.on('pageerror', e => errors.push(e.message));
  async function api(user, url) {
    const r = await fetch(base+url, {headers:{Authorization:'Bearer '+keys[user]}});
    return {status:r.status, data:await r.json()};
  }
  async function login(user) {
    if (await page.locator('#logout').isVisible()) await page.locator('#logout').click();
    await page.locator('#username').fill(user);
    await page.locator('#password').fill(passwords[user]);
    await page.locator('#connect button').click();
    await page.locator('#workspace').waitFor({state:'visible'});
    await page.waitForFunction(u=>document.querySelector('#identity').textContent.endsWith(u),user);
    await page.waitForFunction(()=>!document.querySelector('#connect button').disabled);
    if(['manager','admin'].includes(user)){await page.locator('#nav-support').click();await page.waitForFunction(()=>document.querySelector('#page-title').textContent==='审批与处理');}
  }
  async function create(title, description) {
    await page.locator('#new-ticket').click();
    await page.locator('#title').fill(title);
    await page.locator('#description').fill(description);
    const response = page.waitForResponse(r=>r.url()===base+'/portal/tickets' && r.request().method()==='POST');
    await page.locator('#submit-ticket').click();
    const s = await (await response).json();
    await page.locator('#create-dialog').waitFor({state:'hidden'});
    await page.locator('#detail h2').filter({hasText:title}).first().waitFor();
    return s;
  }
  try {
    await page.goto(base);
    await page.screenshot({path:path.join(outputDir,'landing.png'),fullPage:true});
    await page.waitForFunction(()=>!document.querySelector('#connect button').disabled);
    await page.locator('#username').fill('alice');
    await page.locator('#password').fill('invalid-password-for-acceptance');
    await page.locator('#connect button').click();
    await page.locator('#login-error').filter({hasText:'账号或密码不正确'}).waitFor();
    assert.equal(await page.locator('#workspace').isVisible(),false);
    checks.invalid_identity_rejected = true;
    await login('alice');
    assert.equal(await page.locator('#password').inputValue(),'');
    assert.equal(await page.evaluate(()=>localStorage.length+sessionStorage.length),0);
    checks.password_not_retained_in_page = true;
    await page.reload();await page.locator('#workspace').waitFor({state:'visible'});checks.session_restored_after_reload=true;
    const unclear = await create('系统打不开 '+Date.now(),'现在打不开，不知道是什么原因');
    assert.equal(unclear.status.code,'needs_input');
    await page.getByLabel('补充具体情况').fill('ERP 以前可以使用，现在打开页面报错超时，请排查。');
    await page.getByRole('button',{name:'提交补充信息',exact:true}).click();
    await page.waitForFunction(()=>document.querySelector('#detail .badge')?.textContent==='待支持核查');
    const clarified = (await api('alice','/tickets/'+unclear.id+'/state')).data;
    assert.equal(clarified.ticket.status,'closed');
    assert.ok(clarified.action_result.downstream_id);
    checks.clarification_to_http_incident = true;
    const permission = await create('申请报表数据库权限 '+Date.now(),'我需要申请报表数据库的只读权限，用于查看工作数据。');
    assert.equal(permission.status.code,'confirmation');
    await page.getByRole('button',{name:'确认申请信息',exact:true}).click();
    await page.waitForFunction(()=>document.querySelector('#detail .badge').textContent==='等待审批');
    assert.equal(await page.getByRole('button',{name:'同意申请'}).count(),0);
    checks.applicant_cannot_approve = true;
    await login('manager');
    await page.locator('#ticket-list button').filter({hasText:permission.title}).click();
    await page.getByRole('button',{name:'同意申请'}).click();
    await page.waitForFunction(()=>document.querySelector('#detail .badge').textContent==='申请已处理');
    checks.independent_approval = true;
    await login('bob');
    assert.equal((await api('bob','/tickets/'+permission.id+'/state')).status,404);
    assert.equal(await page.locator('#ticket-list button').count(),0);
    assert.ok((await page.locator('#detail').textContent()).includes('选择一张工单'));
    checks.identity_isolation = true;
    await login('alice');
    const postKeys=[];let drop=true;
    await page.route(base+'/portal/tickets', async route=>{
      if(route.request().method()!=='POST')return route.continue();
      postKeys.push(route.request().headers()['idempotency-key']);
      if(drop){drop=false;await route.fetch();await route.abort('failed');}
      else await route.continue();
    });
    await page.locator('#new-ticket').click();
    const retryTitle='网络中断后重试 '+Date.now();
    await page.locator('#title').fill(retryTitle);
    await page.locator('#description').fill('VPN 以前正常使用，现在连接失败，错误提示连接超时。');
    await page.locator('#submit-ticket').click();
    await page.getByRole('button',{name:'继续确认本次提交'}).waitFor();
    assert.equal(await page.locator('#title').isDisabled(),true);
    await page.locator('#submit-ticket').click();
    await page.locator('#create-dialog').waitFor({state:'hidden'});
    assert.equal(postKeys.length,2);assert.equal(postKeys[0],postKeys[1]);
    const tickets=(await api('alice','/tickets')).data.items;
    assert.equal(tickets.filter(t=>t.title===retryTitle).length,1);
    await page.unroute(base+'/portal/tickets');
    checks.lost_response_same_key_one_ticket = true;
    const unsafe='<img src=x onerror=window.XSS=1>';
    await create(unsafe,'系统打不开，请协助确认是什么问题。');
    assert.equal(await page.evaluate(()=>window.XSS),undefined);
    assert.equal(await page.locator('#detail img').count(),0);
    checks.user_text_not_executed = true;
    await page.getByRole('button',{name:'撤销工单',exact:true}).click();
    await page.getByLabel('撤销原因').fill('浏览器验收结束，取消测试申请');
    await page.getByRole('button',{name:'确认撤销',exact:true}).click();
    await page.waitForFunction(()=>document.querySelector('#detail .badge').textContent==='已撤销');
    checks.cancel_pending_request = true;
    await page.locator('#ticket-list button').filter({hasText:unclear.title}).click();
    if (process.env.BROWSER_REPORT_FIXTURE==='1') {
      await login('manager');
      await page.locator('#ticket-list button').filter({hasText:'真实模型报告回放（测试夹具）'}).click();
      await page.getByRole('heading',{name:'支持人员处理区'}).waitFor();
      await page.getByText('查看排查依据',{exact:true}).click();
      assert.ok(await page.locator('#detail .evidence-item').count()>=1);
      await page.getByLabel('排查建议的核查结论').selectOption('inconclusive');
      await page.getByLabel('核查说明').fill('浏览器功能验收，仅检查反馈记录，未进行真实业务人工评审。');
      await page.getByLabel('核查凭证或记录编号').fill('browser-ui-fixture-only');
      await page.getByRole('button',{name:'提交核查反馈',exact:true}).click();
      await page.locator('#detail .evidence-item').filter({hasText:'浏览器功能验收，仅检查反馈记录'}).waitFor();
      checks.live_report_rendering_and_fixture_feedback = true;
    }
    await page.screenshot({path:path.join(outputDir,'workbench-desktop.png'),fullPage:true});
    await page.setViewportSize({width:390,height:844});
    await page.screenshot({path:path.join(outputDir,'workbench-mobile.png'),fullPage:true});
    assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
    checks.mobile_no_horizontal_overflow = true;
    assert.deepEqual(errors,[]);checks.no_browser_script_errors = true;
    fs.writeFileSync(path.join(outputDir,'result.json'),JSON.stringify({passed:true,scope:'Isolated password-login portal with real independent HTTP demo service; live model verified separately.',checks},null,2));
    console.log(JSON.stringify({passed:true,checks}));
  } catch(e) {
    await page.screenshot({path:path.join(outputDir,'failure.png'),fullPage:true});
    fs.writeFileSync(path.join(outputDir,'result.json'),JSON.stringify({passed:false,checks,error:e.message,errors},null,2));
    throw e;
  } finally {await browser.close();}
}
main().catch(e=>{console.error(e.message);process.exitCode=1;});
