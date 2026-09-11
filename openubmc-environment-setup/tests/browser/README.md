# Local configuration browser tests

Run with Python 3.12 and Node.js 20 or newer:

```bash
npm ci --ignore-scripts --no-audit --no-fund
npx playwright install --with-deps chromium
npm test
```

The suite starts the local configuration server with a temporary configuration
directory, drives its page in Chromium, and checks the saved public API state.
It uses synthetic credentials and does not connect to a device.

To use an existing Chromium installation, set
`OPENUBMC_TEST_CHROMIUM_EXECUTABLE` to its executable path when running `npm test`.
