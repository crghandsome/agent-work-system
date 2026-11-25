# 本地模型 HTTP 服务：Transformers.js 加载 bge-m3 / bge-reranker-large ONNX
FROM node:20-slim

WORKDIR /srv/model-service

# 先拷 package.json 利用层缓存
COPY package.json ./
# onnxruntime-node 的 postinstall 会尝试下载 CUDA 版二进制（GitHub 404），跳过 CUDA，用 CPU 版即可
ENV onnxruntime_node_install_cuda=skip
ENV ONNXRUNTIME_NODE_INSTALL_CUDA=skip
RUN npm install --omit=dev --no-audit --no-fund --onnxruntime-node-install-cuda=skip

COPY server.js ./

EXPOSE 8090
CMD ["node", "server.js"]
