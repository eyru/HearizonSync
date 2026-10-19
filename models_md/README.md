# 模型目录

本目录只放说明，模型文件体积大，不纳入 Git。

## 需要两个模型

| 模型 | 体积 | 用途 |
|---|---|---|
| SenseVoice-Small int8 | 239 MB | 语音识别 |
| opus-mt-en-zh CT2 int8 | 155 MB | 本地翻译 |

## 安装包用户

**不用管** —— 安装包已内置全部模型。

## 源码用户

按主 README 的「模型下载」章节操作。

## 放置位置

```
models/
├── sv/
│   ├── model.int8.onnx
│   └── tokens.txt
└── opus_en_zh/            ← 必须是纯 ASCII 路径！
    ├── model.bin
    ├── source.spm
    ├── target.spm
    ├── shared_vocabulary.json
    └── vocab.json
```

> ⚠️ `opus_en_zh` 若放在含中文的路径下，`sentencepiece` 会报 `NOT_FOUND`。
> 可改用环境变量指定：`set SLM_OPUS_EN_ZH_DIR=D:\slm-models\opus_en_zh`
