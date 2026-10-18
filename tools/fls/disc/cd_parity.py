"""CD-ROM MODE1 扇区 EDC/ECC 的已知答案向量与参考扇区来源。

公开仓库面向的 Steam 版游戏不带原始压制盘的 EDC/ECC 校验字节(已实测 Steam
数据轨抽样 90 个扇区全部不满足 CD parity), 社区成员也只有 Steam 版, 拿不到
原版 Saturn 光盘镜像。因此本模块提供一份确定性的合成已知答案向量(known-answer
vector): 它只由同步头、扇区头和 sha256 计数器派生的伪随机用户数据构成, 绝不
内含任何游戏数据, 用来验证 build_alpha.finish_sector 的 CD-ROM MODE1
EDC/ECC 实现与规范一致。

私有工作区仍然优先使用真实压制盘: 当 WORK/reference_original.bin(原版光盘
裸镜像)存在时, pressed_reference() 读其首扇区并标记 'pressed-disc'; 只有该
文件缺失时才退回合成向量并标记 'known-answer'。

本模块 import 时无副作用: 不读 sys.argv、不打印、不改 sys.stdout, 也不写任何
文件; 唯一访问的路径 WORK/reference_original.bin 限制在 WORK 内。
"""
import hashlib

import build_alpha
from path_compat import WORK

# 合成用户数据的域分隔常量, 更换它即得到另一套互不相关的已知答案向量。
_KAT_DOMAIN = b'farland-saga-cd-parity-kat-v1'


def synthetic_sector():
    """构造确定性合成扇区(finish_sector 之前), 返回 2352 字节。

    布局: 12 字节同步头(00 + FF*10 + 00), 4 字节扇区头(分:秒:帧取 00:02:00,
    即 LBA 0 的 MSF, 第 16 字节为 mode 0x01, 满足 finish_sector 对
    sector[15] == 1 的要求), 2048 字节用户数据, 8 字节零, 其余补零到 2352。
    用户数据第 i 字节(i = 0..2047)取 sha256(_KAT_DOMAIN 拼接 i 的 4 字节大端
    计数器) 摘要的第一个字节, 完全确定且与游戏数据无关。偏移 2064 起的 8 字节
    零是 MODE1 保留字段, finish_sector 会用 EDC 覆盖 2064..2068 并把
    2068..2076 规范化为零。
    """
    sector = bytearray(2352)
    sector[0:12] = b'\x00' + b'\xff'*10 + b'\x00'
    sector[12:16] = bytes((0x00, 0x02, 0x00, 0x01))
    for i in range(2048):
        sector[16+i] = hashlib.sha256(_KAT_DOMAIN + i.to_bytes(4, 'big')).digest()[0]
    sector[2064:2072] = bytes(8)
    return bytes(sector)


# synthetic_sector() 经 build_alpha.finish_sector 计算的完整 2352 字节结果,
# 以十六进制字符串保存(共 4704 字符), 可用 bytes.fromhex 还原。
KNOWN_ANSWER_SECTOR = (
    # 12 字节同步头 + 4 字节扇区头(00:02:00, mode 1)
    '00ffffffffffffffffffff0000020001'
    # 2048 字节确定性伪随机用户数据, 每行 256 字节
    'fd7b134436718032c372ed09f93667bada8541cd8e2990bc604de76af6eb38342579ca653ea32e2eba68ba427335c5c8496f6983a91e3f8e1e71b76335d5033098cfff15a629138af469aba25f25195ff4da1ce35dda4dec8643bee66a7992a64b7ce8efd8d05aadc7eaab46da616d8d30d5a18101f27c04adda82860f64177d3eb6c12663a7ac59c7bbfacef539d539a3235f4c16fcb37ceb6ce685624271f22e08c8000106d95d2f3f59887e520e51a1285e4a9738c42c6f7330971fd26045129e2c45a612438875f9c4ef0fd6beb6b0042f0b7303b5a6f0d4bd7e5c69a3102f0f1d29de44d26b9984e12f47d0d2a7db822e21b0f929c11ba1a9e33bc3efc2'
    'df48e91863ccb12feed45e39c749b1d1471b849c1d438db7d59ba4e9395c595100f9945dbbf1c6587e893b13ffea3200b3b85e2045067840c115e2aaa68d31b6aeea55967c036a436d243c20b7bc923e3444837a2053c328d4d595b04971069a319e1760bc30ce1e909bf70d64950f193447263cb9e3c291409a40f2df9683ec33c08d6f11ab09852d6987f20abd6726afdd0c5c42c8bb6aa14f504c70be842c2b3a4c2f51f35e0a2fcea752d04be424748c566d2e3cd44b045cb83e5db518b24086bfbd8f529fdcd4c4c11f0b895c5d62583fd278257454fe0fe531cb45f93281c2d18ffc81564300852b0aa802fd002643c098213178309b7431d15e7a12ac'
    'a916aeed56255f7517ba6a4805eab44b39433922105e70e120bfc1d7457431092d38664c35c237dee33aaf4a7eef5e334540754b54eec194a53850a789b35de5bb3aa8719caffef194513c54314052a0c46e4e3eaa0d1c58ee54590ba998628869e31cfd9752c8a00de6d0f13007f3821410343c257fb27d05fdb3ee49e47bf25adec795640dde8e608c1a404093d14b56b47f895927e321df393495ce109c843c757dda465fdb2a03ae5043927b331e337b4d8aadf9460061b81dfde1113b148699b69a7b0a6628dbe72e800a83926db614013fd811b5e93b568bb2f1eb5f045a75e930bc3dd3ac3106d24a3845da4e52d2a3e1f2ae42ff6506de1bc0af802f'
    '0c93f7ae3b3429768921c76627985a8ffd788dce576af47a9a464df1e791edb5c30b55a0f310b907a1a932e5311f77ef3d1aeddbd9e1e5cfb79d417bb40f8177e29c8d8a0201cf6d11d7ddae5a591919d45560541188ce46b25d59d8af37dcb739b3db468f65bac7efbdb7ec1af8d2c1b28dfd80be917a3eca2548a64a327168a19f257aa116ebb96a92da7bf7fc16d83e0b44e69d365a4226428549aae8d73bd5eaff4526fa53af205d0820f0ed27e7109cc28f8d4dc9fd6a1fccb69f51cffc611d88aebf7ae75bf55998077876dfb50b7d5560af7eb1b4ed8d9b030185dd9b9798fe6cc5b280618660cafe625ba0ae511c6fe692898f444e5d711a72b84e78'
    '39d0b3020ad249d7bcabd86e174dfcb0357055550a08d0e74d1f2baec4853e715ac217f05c3d0c16e13293dc970893da76611a418535588680acf25756c849de12d6fc117a67a3511295f435ff87b2bd9ab6bf0dca516d4a5013aa748bd0580d36bd11bdb3eee3a9cafe0e0dccb94ea5ec916e971a408289784de6f2b25a54cc1e93473d2a486796a5d8c46876106bd5176628a4c9fde2f6516666b3f4ed5120c4700226d9abbfa1683b54597c69e3496cb3acc6f7ee8348771eeb380d8ba961f632902ee0cb0e68f524fef8307671a8707ab970f301038d170430e4b99b80404d31163d6e39e92177775d8f98ed2da0ded699c1dc3a207df3cde2f4fb6e1130'
    '941b78da0d744fccd638e50d4209fd5cf773c30007687e6e3312c012394d3fedbc02219624f14a67342a1a76426b0d1c213f6380e0bf2fc6106eed583f143041dd5fc64dac3f88c56d8049931ba6d75b63dc807e953577b50cb38b794872c9707166299ed99a4b6dd3a1a63c2d99249fe4f2edab8622a5f8d4c3f0da7f5bc8ea53e8c602b9bfe34717019c9bd41cec721bebd83ae22a08d110ac757012d985ab1b542140f51c26e48137d8fc4af9269c358feaff087a7b2d20ca344374ea638d270594d60d811208bd1fec9a4d11bc39d96e2a404f7923362246ac801588dc11f885d5275d0361d2e4d7e487463a5ac51a466d8c7c58624462dcba1ac99e11c1'
    'a925735123262a4960cc6a014263a8635c04356d8387844a8d65238f41b3ca7b028bb4ab4d9f7667d86d9e19eddf51eeb3def02cc75c9721a444d049c19c4e447370ecc011de33d652fe1b510fc44cc7823a7786893d6eb7a2aa8117592b7ed0cfdbf73e5f1c2d79a4bea5e3adca09a1714ed5f7e836cf8f6e1ef7cdd2f1ef99c42403a0fc22d2d7bf1fa9e40b6809b397b233279ae1210e750c2a742813e8d2a5553d524229359ccf4a1fdf9abd44501009fc458fcf8123e50b0546e4cd6f002c0acd4874e4d44b2a4f8f78ecc6826607fd4e0a9412b229e9bf602501bc25d141d13192f1ad1e3399d4d1cd670c3114c1ed66053d0cbbe778f05a68265517bd'
    '7b464b6163582a93890e35ed43ea57ce7ba66a8251de5f5601b04f78696c9aff4936f62be7cebf9eac37e52b7c6b66f1dc82a0fe6d0c62474f4da4def75486edd4b8a8e1bc2c0fee4b8cf47267f479fd94a6f5fcd082fb8891358181d4553bb972cd33cdb3b709f4f38fc53a1b6829134549df8beb5937046e2bcd03b7c9e8b1f324eca68902516e395b778a438f4edcba0f5140b2e45a15bb956f97dcef41cbbca1c13a0e09040baf75ebea41978b6709eb2f325504f15de59bae31a73b0dc8e0670aea749758834e459b5ffadd722d77f7fd0e5722e576f7f6810460e80fdd7cb1455f61080b2c4c393b2f72e55c0cc2ec7dafa72f902e45f38a9cc41310a8'
    # EDC(4 字节小端) + 8 字节保留零
    '6a06b8bc0000000000000000'
    # P 校验 172 字节: 前 86 字节为 x 系数, 后 86 字节为常数项
    '8e103c36154d72ae2c19e17f6c1d69a7887e2f1c5181fb5f9617013c2888f687e704618ef4dc8617d20968b396bed08c7250a3a61f231de97db14a771bcd8bc15238f71f4704549cadfb8f5ab27510199fe5aa1b1f5f'
    '96d63d2f9cea09222468a6a49c2a31e669f2124e63bf3f0086e5e5180403b8c9b49a2ffbf9e0ceffb3fc0874b0fb7dc4adcc3d5fa2eba9fed0be93fab47678c5f82679fcca371711dc284a4eaeae64e07921e248a43b'
    # Q 校验 104 字节: 前 52 字节为 x 系数, 后 52 字节为常数项
    '6e9803575b4e7b90757c6d751f0d79af2219dc874db7121474ca4df2c285df93dbfc54eea92506a02a844ab46ac25cc36026a029'
    '0efaf077504b8cac1912e5932bab182d6e9a006655f1987aa31a843947306c79bdfea88861d2bb57b8a5b73b3c46ce8f331443c0'
)


def pressed_reference():
    """返回 (扇区字节, 来源标签)。

    私有工作区的原版光盘裸镜像 WORK/reference_original.bin 存在时, 读其首
    2352 字节并标记 'pressed-disc'; 否则返回合成已知答案向量并标记
    'known-answer'。只读取 WORK 内的这一个路径, 不写任何文件。
    """
    reference = WORK / 'reference_original.bin'
    if reference.is_file():
        return reference.read_bytes()[:2352], 'pressed-disc'
    return bytes.fromhex(KNOWN_ANSWER_SECTOR), 'known-answer'


def verify(sector_bytes, source_label):
    """对扇区重算 EDC/ECC 并断言往返一致, 失败时抛出带来源标签的中文错误。"""
    sector = bytearray(sector_bytes)
    try:
        assert len(sector) == 2352 and sector[15] == 1, '扇区长度不是 2352 或 mode 字节不是 1'
        assert build_alpha.finish_sector(sector) == bytes(sector_bytes), 'EDC/ECC 往返不一致'
    except AssertionError as error:
        raise ValueError(f'CD 扇区校验失败(来源 {source_label}): {error}') from None
    return bytes(sector)
