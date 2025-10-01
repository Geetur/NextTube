FROM node:20-alpine

WORKDIR /app
COPY package.json ./
COPY tsconfig.json ./
COPY next.config.js ./
RUN npm install

COPY app ./app

EXPOSE 3000
CMD ["npm", "run", "dev"]
