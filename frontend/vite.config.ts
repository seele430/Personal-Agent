
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],

  server: {
    host: "127.0.0.1",
    port: 5173,

    proxy: {
      // 后端路由本身带 /api 前缀，代理**不能再剥**（2026-11-08 修复：
      // 剥前缀会让 /api/* 全部匹配不到，见 pa-v122-evidence 的 BLOCKER 文档）。
      "/api": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
      },
    },
  },
});