/** Workspace production UI with synthetic OIDC/API fixtures; no live/cloud verification. */
import { chromium, expect } from '@playwright/test';
import { createServer } from 'node:http';
import { readFile, mkdir, mkdtemp, rm } from 'node:fs/promises';
import { join, extname } from 'node:path';
import { tmpdir } from 'node:os';
const root = new URL('../telemetry-ui/dist/telemetry-ui/browser/', import.meta.url);
const evidence = process.env.WO_BROWSER_EVIDENCE;
if (!evidence) throw new Error('WO_BROWSER_EVIDENCE is required');
await mkdir(evidence, {recursive:true});
const server = createServer(async (request,response) => {
  try {
    const pathname = new URL(request.url, 'http://localhost').pathname;
    const file = pathname.startsWith('/work-organizer') || pathname === '/' ? 'index.html' : pathname.slice(1);
    if (file.includes('..')) { response.writeHead(400).end(); return; }
    const body = await readFile(new URL(file,root));
    response.setHeader('Content-Type', {'.js':'text/javascript','.css':'text/css','.html':'text/html','.json':'application/json'}[extname(file)] ?? 'application/octet-stream');
    response.end(body);
  } catch { response.writeHead(404).end(); }
});
await new Promise(resolve => server.listen(4318, '127.0.0.1', resolve));
const base = 'http://127.0.0.1:4318';
const issuer = 'https://cognito-idp.ca-central-1.amazonaws.com/ca-central-1_fixture';
const domain = 'https://fixture.auth.ca-central-1.amazoncognito.com';
const profile = await mkdtemp(join(tmpdir(), 'wo-workspace-browser-'));
let context;
try {
  const executablePath = process.env.WO_BROWSER_EXECUTABLE ??
    (process.platform === 'darwin' ? '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' : undefined);
  context = await chromium.launchPersistentContext(profile, {
    ...(executablePath ? {executablePath} : {}),
    headless: process.env.WO_NATIVE !== '1', viewport: {width:1440,height:900},
  });
  let nonce = ''; let failList = false; let failRead = false; let signedIn = false;
  let analyticsEventPresent = true;
  let authOrigin = Math.floor(Date.now() / 1000);
  const csrfToken = 'a'.repeat(43);
  const session = () => ({
    issuer,
    sub: 'alice',
    groups: ['portal-users'],
    displayName: 'Alex Morgan',
    expiresAt: authOrigin + 90 * 86400,
    csrfToken,
  });
  const doc = {owner:'alice', file_id:'11111111-1111-4111-8111-111111111111', filename:'Welcome.md', category:'learning', created_at:'2026-10-27', size_bytes:50,status:'READY'};
  const secondDoc = {owner:'alice', file_id:'33333333-3333-4333-8333-333333333333', filename:'Release-notes.md', category:'workflow-docs', created_at:'2026-11-01', size_bytes:80,status:'READY'};
  const portfolioEvent = {
    event_id: '22222222-2222-4222-8222-222222222222',
    visitor_key: '0123456789abcdef'.repeat(4),
    visited_at: '2026-11-02T08:00:00Z',
    client_timestamp: '2026-11-02T08:00:00Z',
    event_type: 'page_view',
    target: 'Portfolio',
    project: 'Runtime Controller',
    page_title: 'Runtime Controller',
    page_path: '/projects/runtime-controller',
    status: 'served',
    location: {city:'Oakville',region:'ON',country:'CA',timezone:'America/Toronto'},
    metadata: {language:'en-CA',referrer_host:'direct'},
  };
  const requests = []; const errors = [];
  await context.route('**/auth-config.json', route => route.fulfill({json:{issuer,clientId:'fixtureclient',domain,redirectUri:base+'/work-organizer/auth/callback',logoutUri:base+'/work-organizer/signed-out'}}));
  await context.route(domain+'/oauth2/authorize**', async route => {
    const url = new URL(route.request().url()); nonce = url.searchParams.get('nonce');
    await route.fulfill({status:302,headers:{location:base+'/work-organizer/auth/callback?code=fixture&state='+encodeURIComponent(url.searchParams.get('state'))}});
  });
  await context.route(domain+'/logout**', route => route.fulfill({status:302,headers:{location:base+'/work-organizer/signed-out'}}));
  await context.route(domain+'/oauth2/token', route => {
    const encode = value => Buffer.from(JSON.stringify(value)).toString('base64url');
    const now = Math.floor(Date.now()/1000); authOrigin = now;
    const token = encode({alg:'RS256',typ:'JWT'})+'.'+encode({iss:issuer,aud:'fixtureclient',sub:'alice',name:'Alex Morgan',email:'alex@example.test',iat:now,auth_time:now,exp:now+300,nonce})+'.fixture';
    return route.fulfill({json:{access_token:'fixture-only',refresh_token:'fixture-refresh',id_token:token,token_type:'Bearer',expires_in:300}});
  });
  await context.route('**/api/**', async route => {
    const url = new URL(route.request().url()); requests.push(url.pathname+url.search);
    if (!route.request().headers()['x-device-id']) return route.fulfill({status:403,json:{error:'device_required'}});
    if (url.pathname === '/api/session') {
      if (route.request().method() === 'POST') {
        signedIn = true;
        return route.fulfill({json:session()});
      }
      return signedIn
        ? route.fulfill({json:session()})
        : route.fulfill({status:401,json:{error:'browser_session_required'}});
    }
    if (url.pathname === '/api/session/logout') {
      if (route.request().method() === 'POST') {
        signedIn = false;
        return route.fulfill({json:{success:true}});
      }
      return signedIn
        ? route.fulfill({json:{csrfToken}})
        : route.fulfill({status:401,json:{error:'browser_session_required'}});
    }
    if (url.pathname==='/api/me') return route.fulfill({json:{issuer,sub:'alice',groups:['portal-users']}});
    if (url.pathname==='/api/projects') return route.fulfill({json:{projects:[{id:'project-1',name:'Work Organizer',description:'Telemetry work',created_at:'2026-11-02T08:00:00Z',owner:'alice'}],cursor:null}});
    if (url.pathname==='/api/tasks') return route.fulfill({json:{tasks:[],cursor:null}});
    if (url.pathname==='/api/analytics/gh-portfolio') {
      if (route.request().method()==='DELETE') {
        expect(route.request().postDataJSON()).toEqual({events:[{visitor_key:portfolioEvent.visitor_key,event_id:portfolioEvent.event_id}]});
        analyticsEventPresent = false;
        return route.fulfill({json:{removed:[{visitor_key:portfolioEvent.visitor_key,event_id:portfolioEvent.event_id}],deleted_count:1,already_absent_count:0}});
      }
      return route.fulfill({json:{events:analyticsEventPresent?[portfolioEvent]:[],next_cursor:null,as_of:'2026-11-02T08:05:00Z'}});
    }
    if (url.pathname==='/api/documents') {
      if (failList) { failList=false; return route.fulfill({status:503,json:{error:'fixture_failure'}}); }
      return route.fulfill({json:{documents:url.searchParams.get('scope')==='mine'?[]:[doc,secondDoc],cursor:null}});
    }
    if (failRead) { failRead=false; return route.fulfill({status:404,json:{error:'not_found'}}); }
    return route.fulfill({json:{document:{...doc,content:'# Welcome\nSafe document text.\n<script>alert("unsafe")</script>'}}});
  });
  const page = context.pages()[0];
  page.on('pageerror', error => errors.push(error.message));
  const board = '/work-organizer/workspace/project-tracker';
  async function capture(name) {
    await page.evaluate(() => window.scrollTo(0, 0));
    await page.waitForFunction(() => document.getAnimations().every(animation =>
      animation.effect?.getComputedTiming().iterations === Infinity ||
      animation.playState === 'finished' || animation.playState === 'idle'));
    await page.screenshot({path:join(evidence,name),fullPage:true});
  }
  for (const viewport of [{width:1440,height:900},{width:390,height:844}]) {
    analyticsEventPresent = true;
    await page.setViewportSize(viewport);
    await page.goto(base+'/');
    await expect(page).toHaveURL(/\/work-organizer\/login/);
    await expect(page.locator('.wo-header, .wo-sidebar, .wo-footer')).toHaveCount(0);
    const login = await page.locator('wo-auth-page wo-card').boundingBox();
    expect(Math.abs(login.x + login.width / 2 - viewport.width / 2)).toBeLessThan(3);
    expect(Math.abs(login.y + login.height / 2 - viewport.height / 2)).toBeLessThan(3);
    await capture(`login-${viewport.width}.png`);
    await page.getByRole('button',{name:'Sign in',exact:true}).click();
    await expect(page).toHaveURL(base+board);
    await expect(page.locator('.wo-header-nav').getByRole('link',{name:'Design system'})).toHaveCount(0);
    await expect(page.locator('.wo-footer')).toBeHidden();
    await expect(page.getByRole('button',{name:/Alex Morgan.*Account settings/})).toBeVisible();
    await page.getByRole('button',{name:/Alex Morgan.*Account settings/}).click();
    await expect(page.getByRole('region',{name:'Account settings'})).toBeVisible();
    await page.keyboard.press('Escape');
    await expect(page.getByRole('region',{name:'Account settings'})).toHaveCount(0);
    const title = `Release review ${viewport.width}`;
    await page.goto(base+board+'/new');
    await page.locator('#work-title').fill(title);
    await page.locator('#work-project').fill('Work Organizer');
    await page.locator('#work-description').fill('# Acceptance\n\n**Ready for review**\n\n- Check login\n- Read documents');
    await page.getByRole('tab',{name:'Preview',exact:true}).click();
    await expect(page.locator('wo-markdown h1')).toHaveText('Acceptance');
    await expect(page.locator('wo-markdown strong')).toHaveText('Ready for review');
    await page.getByRole('button',{name:'Create work item',exact:true}).click();
    await expect(page).toHaveURL(base+board);
    const card = page.getByRole('button',{name:`Edit ${title}`,exact:true});
    await expect(card).toBeVisible();
    await card.click();
    await page.getByRole('combobox',{name:'Status'}).click();
    await page.keyboard.press('ArrowDown');
    await page.keyboard.press('Enter');
    await page.getByRole('button',{name:'Save work item',exact:true}).click();
    await expect(page.locator('dialog.tm-item-drawer[open]')).toHaveCount(0);
    await page.getByRole('button',{name:`Edit ${title}`,exact:true}).click();
    await page.getByRole('combobox',{name:'Status'}).click();
    await page.keyboard.press('ArrowDown');
    await page.keyboard.press('Enter');
    await page.locator('#item-title').fill(title+' updated');
    await page.getByRole('button',{name:'Save work item',exact:true}).click();
    await expect(page.locator('dialog.tm-item-drawer[open]')).toHaveCount(0);
    await expect(page.locator('.wo-kanban-column').filter({has:page.getByRole('heading',{name:'Done',exact:true})}).getByRole('button',{name:`Edit ${title} updated`,exact:true})).toBeVisible();
    const layout = await page.evaluate(() => {
      const header = document.querySelector('.wo-header');
      const headerActive = document.querySelector('.wo-header-nav .active');
      return {header:getComputedStyle(header).backgroundColor,
        active:getComputedStyle(headerActive,'::after').backgroundColor,
        overflow:document.documentElement.scrollWidth>innerWidth};
    });
    expect(layout.active).not.toBe('rgba(0, 0, 0, 0)'); expect(layout.overflow).toBe(false);
    console.log(`Layout ${viewport.width}:`, layout);
    await capture(`tracker-${viewport.width}.png`);
    await page.reload();
    await expect(page).toHaveURL(base+board);
    await expect(page.getByRole('button',{name:`Edit ${title} updated`,exact:true})).toBeVisible();
    await page.locator('.wo-header-nav').getByRole('link',{name:'Documents',exact:true}).click();
    await expect(page.getByRole('heading',{name:'Documents (2)'})).toBeVisible();
    const documentRowStyle = await page.locator('.tm-document-row').first().evaluate(row => {
      const item = row.querySelector('.tm-document-item');
      const style = getComputedStyle(item);
      return {background:style.backgroundColor,borderTop:style.borderTopWidth,borderRight:style.borderRightWidth,separator:getComputedStyle(row).borderBottomWidth};
    });
    expect(documentRowStyle.background).toBe('rgba(0, 0, 0, 0)');
    expect(documentRowStyle.borderTop).toBe('0px');
    expect(documentRowStyle.borderRight).toBe('0px');
    expect(documentRowStyle.separator).toBe('1px');
    await page.getByRole('button',{name:/Welcome.md/}).click();
    await expect(page.locator('.wo-document-reader wo-markdown h1')).toHaveText('Welcome');
    await expect(page.locator('.wo-document-reader script')).toHaveCount(0);
    await page.getByRole('searchbox',{name:'Search documents'}).fill('Missing document');
    await expect(page.getByText('No matching documents.')).toBeVisible();
    await page.getByRole('searchbox',{name:'Search documents'}).fill('');
    failList=true;
    await page.reload();
    await expect(page.getByRole('alert')).toContainText('could not be loaded');
    await page.getByRole('button',{name:'Retry',exact:true}).click();
    await expect(page.getByRole('button',{name:/Welcome.md/})).toBeVisible();
    failRead=true;
    await page.getByRole('button',{name:/Welcome.md/}).click();
    await expect(page.getByRole('alert')).toContainText('could not be opened');
    await page.getByRole('button',{name:'Retry opening'}).click();
    await expect(page.locator('.wo-document-reader wo-markdown h1')).toHaveText('Welcome');
    await capture(`documents-${viewport.width}.png`);
    await page.locator('.wo-header-nav').getByRole('link',{name:'Merger agent',exact:true}).click();
    await expect(page).toHaveURL(base+'/work-organizer/workspace/merger-agent');
    const documentsTab = page.locator('#merger-documents-tab');
    const mergedTab = page.locator('#merger-merged-tab');
    await expect(documentsTab).toHaveAttribute('aria-selected','true');
    await documentsTab.press('ArrowRight');
    await expect(mergedTab).toHaveAttribute('aria-selected','true');
    await mergedTab.press('ArrowLeft');
    await expect(documentsTab).toHaveAttribute('aria-selected','true');
    await page.getByRole('checkbox',{name:'Select Welcome.md for merger'}).check();
    const mergerSourceRow = page.locator('#merger-documents-panel .tm-document-row').filter({hasText:'Welcome.md'});
    await mergerSourceRow.getByRole('button').click();
    const previewDialog = page.locator('dialog.tm-document-preview-dialog[open]');
    await expect(previewDialog).toBeVisible();
    await expect(previewDialog.locator('wo-markdown h1')).toHaveText('Welcome');
    await capture(`merger-source-preview-${viewport.width}.png`);
    await previewDialog.getByRole('button',{name:'Close',exact:true}).click();
    await expect(previewDialog).toHaveCount(0);
    await page.getByLabel('Merger instructions').fill('Combine the decisions and next steps.');
    await page.getByRole('button',{name:'Merge documents',exact:true}).click();
    await expect(mergedTab).toHaveAttribute('aria-selected','true');
    const mergedDocument = page.getByRole('button',{name:/merged-document-1\.md/});
    await expect(mergedDocument).toBeVisible();
    await mergedDocument.click();
    await expect(previewDialog).toContainText('Combine the decisions and next steps.');
    await capture(`merger-result-preview-${viewport.width}.png`);
    await previewDialog.getByRole('button',{name:'Close',exact:true}).click();
    await expect(previewDialog).toHaveCount(0);
    await capture(`merger-${viewport.width}.png`);
    await page.goto(base+'/work-organizer/analytics/gh-portfolio');
    await expect(page.getByRole('heading',{name:'GH Portfolio',level:2})).toBeVisible();
    await expect(page.getByRole('region',{name:'Portfolio activity table'})).toContainText('Runtime Controller');
    await expect(page.getByRole('region',{name:'Portfolio activity table'})).toContainText('Oakville, ON, CA');
    await page.getByRole('checkbox',{name:/Select Visitor-/}).check();
    await page.getByRole('button',{name:'Delete records (1)',exact:true}).click();
    const deleteDialog = page.getByRole('dialog',{name:'Delete portfolio records?'});
    await expect(deleteDialog).toContainText('Delete 1 activity (1 record)');
    await deleteDialog.getByRole('button',{name:'Delete records',exact:true}).click();
    await expect(page.getByRole('status')).toContainText('1 record deleted');
    await expect(page.getByRole('region',{name:'Portfolio activity table'})).toHaveCount(0);
    await expect(page.getByText('No portfolio activity has been recorded yet.')).toBeVisible();
    await capture(`analytics-${viewport.width}.png`);
    await page.getByRole('button',{name:/Alex Morgan.*Account settings/}).click();
    await page.getByRole('button',{name:'Log out',exact:true}).click();
    await expect(page).toHaveURL(base+'/work-organizer/login');
    await expect(page.locator('.wo-header, .wo-sidebar, .wo-footer')).toHaveCount(0);
    console.log(`PASS fixture ${viewport.width}x${viewport.height}: login, account, create/edit/preview/status/persistence, navigation, Markdown documents/error/retry, merger preview and logout.`);
  }
  expect(errors).toEqual([]);
  if (process.env.WO_NATIVE==='1') {
    console.log('Native login inspection ready; press Enter to close owned browser/server.');
    await new Promise(resolve => {
      process.stdin.once('data',resolve); process.once('SIGTERM',resolve); process.once('SIGINT',resolve);
    });
  }
} finally {
  await context?.close();
  await new Promise(resolve=>server.close(resolve));
  await rm(profile,{recursive:true,force:true});
}
