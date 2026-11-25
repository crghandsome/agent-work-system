/**
 * 本地模型 HTTP 服务：用 Transformers.js 加载本地 ONNX 模型。
 *
 * - POST /embed   {inputs: string[]}  -> number[][]   (bge-m3, 1024维)
 * - POST /rerank  {query, texts}      -> [{index, score}]
 * - GET  /health
 *
 * 模型目录：D:/EmbedingModel/bge-m3, D:/EmbedingModel/bge-reranker-large
 */
import express from 'express';
import { pipeline, AutoTokenizer, AutoModelForSequenceClassification, env } from '@huggingface/transformers';

// 允许本地模型文件，禁用远程访问（完全离线）
env.allowLocalModels = true;
env.allowRemoteModels = false;
env.backends.onnx.wasm.wasmPaths = undefined; // 使用 node 原生 onnxruntime，避免 wasm 下载

const EMBED_MODEL = process.env.EMBED_MODEL || 'D:/EmbedingModel/bge-m3';
const RERANK_MODEL = process.env.RERANK_MODEL || 'D:/EmbedingModel/bge-reranker-large';
const PORT = process.env.PORT || 8090;

let embedder = null;
let rerankTokenizer = null;
let rerankModel = null;

const app = express();
app.use(express.json({ limit: '20mb' }));

app.get('/health', (req, res) => {
  res.json({ status: 'ok', embed: !!embedder, rerank: !!rerankModel,
             embed_model: EMBED_MODEL, rerank_model: RERANK_MODEL });
});

app.post('/embed', async (req, res) => {
  try {
    if (!embedder) {
      console.log('[embed] loading model...');
      embedder = await pipeline('feature-extraction', EMBED_MODEL, { dtype: 'fp16' });
      console.log('[embed] model loaded');
    }
    const inputs = req.body.inputs || [];
    if (!Array.isArray(inputs) || !inputs.length) return res.json([]);
    // mean pooling + normalize，输出 1024 维向量
    const output = await embedder(inputs, { pooling: 'mean', normalize: true });
    // output.tolist(): [n, dim]
    const vectors = output.tolist();
    res.json(vectors);
  } catch (e) {
    console.error('[embed] error', e);
    res.status(500).json({ error: String(e && e.message || e) });
  }
});

app.post('/rerank', async (req, res) => {
  try {
    if (!rerankModel) {
      console.log('[rerank] loading model...');
      rerankTokenizer = await AutoTokenizer.from_pretrained(RERANK_MODEL);
      rerankModel = await AutoModelForSequenceClassification.from_pretrained(RERANK_MODEL, { dtype: 'q8' });
      console.log('[rerank] model loaded');
    }
    const { query, texts } = req.body;
    if (!Array.isArray(texts) || !texts.length) return res.json([]);
    // bge-reranker: 把 query 与每段文本拼成句子对，取 logit 经 sigmoid 作相关性分
    const scores = [];
    for (const t of texts) {
      const inputs = rerankTokenizer(query, { text_pair: t, truncation: true, max_length: 512 });
      const out = await rerankModel(inputs);
      // logits shape [1,1]，取标量做 sigmoid
      const logit = out.logits.data[0];
      scores.push(1 / (1 + Math.exp(-logit)));
    }
    const scored = scores.map((score, i) => ({ index: i, score }));
    scored.sort((a, b) => b.score - a.score);
    res.json(scored);
  } catch (e) {
    console.error('[rerank] error', e);
    res.status(500).json({ error: String(e && e.message || e) });
  }
});

app.listen(PORT, '0.0.0.0', () => {
  console.log(`Model service listening on http://0.0.0.0:${PORT}`);
  console.log(`  embed: ${EMBED_MODEL}`);
  console.log(`  rerank: ${RERANK_MODEL}`);
});
