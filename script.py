const { chromium } = require('playwright');

(async () => {
  const browser = await chromium.launch({ headless: false });
  const context = await browser.newContext();
  const page = await context.newPage();

  // Login
  await page.goto('https://example.com/login');
  await page.fill('#username', 'myuser');
  await page.fill('#password', 'mypass');
  await page.click('button[type="submit"]');
  
  // Wait for login to complete
  await page.waitForURL('**/dashboard');

  // ✅ Save authentication state
  await context.storageState({ path: 'auth.json' });
  
  console.log('Auth state saved to auth.json');
  await browser.close();
})();
