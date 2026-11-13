FROM node:22-alpine AS frontend_build
WORKDIR /webapp
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY index.html vite.config.ts tsconfig*.json ./
COPY src ./src
COPY public ./public
RUN npm run build

FROM nginx:stable-alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=frontend_build /webapp/dist /usr/share/nginx/html
EXPOSE 80
