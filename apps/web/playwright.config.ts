import {defineConfig} from '@playwright/test';
export default defineConfig({testDir:'e2e',timeout:120000,expect:{timeout:20000},use:{baseURL:'http://localhost:4200',screenshot:'only-on-failure',trace:'retain-on-failure'},workers:1});
