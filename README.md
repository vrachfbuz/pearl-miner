# Pearl Miner

Pearl (PRL) miner — NoisyGEMM Proof-of-Useful-Work.

## HiveOS установка

**Installation URL:**
```
https://github.com/vrachfbuz/pearl-miner/releases/latest/download/pearl-miner.tar.gz
```

**Flight Sheet:**
| Поле | Значение |
|------|----------|
| Miner | Custom |
| Installation URL | ссылка выше |
| Hash algorithm | pearl |
| Pool URL | us2.alphapool.tech:5566 |
| Wallet | ваш PRL-адрес |
| Pass | x;d=256 |

## Поддерживаемые GPU

| Архитектура | Карты | Статус |
|-------------|-------|--------|
| CPU режим | любые | ✅ работает |
| sm_75 Turing | CMP 40HX, RTX 20xx | 🔧 в разработке |
| sm_86+ Ampere | RTX 30xx | планируется |

## Ручной запуск

```bash
pip3 install blake3 numpy
python3 miner.py --pool us2.alphapool.tech:5566 --address ВАШ_АДРЕС --worker rig01
```
