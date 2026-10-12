// Lambda PRESIGN: gera uma URL assinada para o navegador subir uma imagem
// DIRETO no S3 (sem passar o binario pela Lambda), na pasta img/ do bucket do site.
// A imagem e servida de volta pelo CloudFront no mesmo dominio (/img/<arquivo>).
//
// Rota: GET /upload-url?filename=<nome>&type=<content-type>
// Resposta: { uploadUrl, publicUrl, key }
//
// Seguranca (demo): aceita apenas content-type de imagem e gera a key com UUID
// (nao da para sobrescrever arquivo de outro). O admin NAO tem autenticacao:
// antes de producao, proteger a rota. Veja o README.

import { S3Client, PutObjectCommand } from "@aws-sdk/client-s3";
import { getSignedUrl } from "@aws-sdk/s3-request-presigner";
import { randomUUID } from "node:crypto";

const REGION = process.env.REGION || process.env.AWS_REGION;
// O Struct8 injeta o NOME do bucket conectado (AWS_S3_BUCKET_NAME_0).
const BUCKET = process.env.AWS_S3_BUCKET_NAME_0 || process.env.BUCKET_NAME;
// Dominio publico por onde as imagens sao servidas (CloudFront). Opcional:
// sem ele, devolvemos uma URL relativa /img/<key>, que funciona no mesmo site.
const PUBLIC_BASE = process.env.PUBLIC_BASE_URL || "";

const client = new S3Client({ region: REGION });

const EXT = {
  "image/jpeg": "jpg",
  "image/png": "png",
  "image/webp": "webp",
  "image/gif": "gif",
  "image/avif": "avif",
};

function response(statusCode, body) {
  return {
    statusCode,
    headers: { "content-type": "application/json; charset=utf-8" },
    body: JSON.stringify(body),
  };
}

export const handler = async (event) => {
  if (!BUCKET) {
    console.error("BUCKET ausente no ambiente");
    return response(500, { error: "bucket nao configurado" });
  }

  const qs = (event && event.queryStringParameters) || {};
  const type = (qs.type || "").toLowerCase();

  if (!EXT[type]) {
    return response(400, {
      error: "tipo de imagem nao suportado",
      aceitos: Object.keys(EXT),
    });
  }

  const ext = EXT[type];
  const key = `img/${randomUUID()}.${ext}`;

  try {
    const uploadUrl = await getSignedUrl(
      client,
      new PutObjectCommand({
        Bucket: BUCKET,
        Key: key,
        ContentType: type,
      }),
      { expiresIn: 300 } // 5 min
    );

    const publicUrl = PUBLIC_BASE ? `${PUBLIC_BASE}/${key}` : `/${key}`;
    return response(200, { uploadUrl, publicUrl, key });
  } catch (err) {
    console.error("falha ao gerar presigned url", err);
    return response(500, { error: "falha ao gerar url de upload" });
  }
};
