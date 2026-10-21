# 每10条处理记录

每条先构建、验证、合并和保存，再开始下一条。下表由每条记录重建，不代表已逐条进行人工语义审核。

| 已完成 | SM概念 | EM | 去重后多步PM | 最后一条 |
|---:|---:|---:|---:|---|
| 10 | 12 | 30 | 9 | incident_5_train_009 |
| 20 | 12 | 53 | 18 | incident_5_train_019 |
| 30 | 12 | 66 | 27 | incident_5_train_029 |
| 40 | 12 | 72 | 36 | incident_5_train_039 |
| 50 | 12 | 75 | 43 | incident_5_train_049 |
| 60 | 12 | 78 | 52 | incident_5_train_059 |
| 70 | 12 | 79 | 61 | incident_5_train_069 |
| 80 | 12 | 81 | 66 | incident_5_train_079 |
| 90 | 12 | 83 | 72 | incident_5_train_089 |
| 100 | 12 | 84 | 78 | incident_5_train_099 |
| 110 | 12 | 85 | 83 | incident_34_train_009 |
| 120 | 12 | 87 | 87 | incident_34_train_019 |
| 130 | 12 | 88 | 89 | incident_34_train_029 |
| 140 | 14 | 92 | 93 | incident_39_train_001 |
| 150 | 14 | 96 | 101 | incident_39_train_011 |
| 160 | 14 | 99 | 108 | incident_39_train_021 |
| 170 | 16 | 102 | 111 | incident_39_train_031 |
| 180 | 16 | 103 | 119 | incident_39_train_041 |
| 190 | 16 | 104 | 125 | incident_39_train_051 |
| 200 | 16 | 106 | 130 | incident_39_train_061 |
| 210 | 16 | 107 | 137 | incident_39_train_071 |
| 220 | 16 | 107 | 141 | incident_39_train_081 |
| 230 | 16 | 107 | 147 | incident_39_train_091 |
| 240 | 16 | 107 | 151 | incident_55_train_001 |
| 250 | 16 | 108 | 156 | incident_55_train_011 |
| 260 | 16 | 110 | 164 | incident_55_train_021 |
| 270 | 17 | 113 | 168 | incident_55_train_031 |
| 280 | 17 | 121 | 176 | incident_55_train_041 |
| 290 | 20 | 130 | 182 | incident_55_train_051 |
| 300 | 20 | 131 | 187 | incident_55_train_061 |
| 310 | 20 | 137 | 193 | incident_55_train_071 |
| 320 | 20 | 137 | 197 | incident_55_train_081 |
| 330 | 20 | 141 | 204 | incident_55_train_091 |
| 340 | 20 | 143 | 209 | incident_134_train_001 |
| 350 | 20 | 150 | 213 | incident_134_train_011 |
| 360 | 20 | 150 | 214 | incident_134_train_021 |
| 370 | 20 | 154 | 216 | incident_166_train_008 |
| 380 | 20 | 155 | 218 | incident_166_train_018 |
| 390 | 20 | 158 | 220 | incident_166_train_028 |
| 400 | 21 | 161 | 224 | incident_322_train_004 |
| 410 | 23 | 166 | 227 | incident_322_train_014 |
| 418 | 23 | 169 | 232 | incident_322_train_022 |
