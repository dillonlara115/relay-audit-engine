# Playwright ships an image with chromium and every system library already
# matched to the driver version. Installing chromium onto a plain node image is
# a long afternoon of missing shared objects.
#
# The tag and the playwright-core version in package.json must move together.
# A mismatched pair launches a browser the driver cannot speak to.
FROM mcr.microsoft.com/playwright:v1.62.1-noble

ENV NODE_ENV=production \
    PORT=8080 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

COPY package.json ./
RUN npm install --omit=dev --no-audit --no-fund

COPY server.js ./

# Cloud Run sets PORT. The service holds one browser and opens a context per
# request, so it is memory hungry and CPU light.
EXPOSE 8080
CMD ["node", "server.js"]
