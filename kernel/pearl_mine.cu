/*
 * pearl_mine.cu  —  Pearl NoisyGEMM CPU+GPU майнер (sm_75 / CMP 40HX)
 *
 * Компиляция:
 *   nvcc -arch=sm_75 -O3 -Xcompiler -fPIC -I./b3 \
 *        pearl_mine.cu blake3_host.o b3/blake3*.o \
 *        -lcudart -lpthread -o pearl_miner
 *
 * Запуск:
 *   ./pearl_miner --pool stratum+tcp://pearl.baikalmine.com:2010 \
 *                 --wallet prl1... --device 0
 */

#include <cuda_runtime.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <netinet/in.h>
#include <arpa/inet.h>
#include <netdb.h>
#include <unistd.h>
#include <pthread.h>
#include <time.h>
#include <math.h>

extern "C" {
#include "b3/blake3.h"
}

/* =========================================================
 * BLAKE3 утилиты (CPU)
 * ========================================================= */

/* XOF: BLAKE3(tag || key).digest(length=n) */
static void blake3_xof(const uint8_t* tag, int tag_len,
                       const uint8_t* key, int key_len,
                       uint8_t* out, size_t out_len)
{
    blake3_hasher h;
    blake3_hasher_init(&h);
    blake3_hasher_update(&h, tag, tag_len);
    blake3_hasher_update(&h, key, key_len);
    blake3_hasher_finalize_seek(&h, 0, out, out_len);
}

/* BLAKE3(data, key=key) → 32 байта */
static void blake3_keyed_hash(const uint8_t* key,
                               const uint8_t* data, size_t data_len,
                               uint8_t* out32)
{
    blake3_hasher h;
    blake3_hasher_init_keyed(&h, key);
    blake3_hasher_update(&h, data, data_len);
    blake3_hasher_finalize(&h, out32, 32);
}

/* BLAKE3(a || b) → 32 байта */
static void blake3_concat(const uint8_t* a, int alen,
                          const uint8_t* b, int blen,
                          uint8_t* out32)
{
    blake3_hasher h;
    blake3_hasher_init(&h);
    blake3_hasher_update(&h, a, alen);
    blake3_hasher_update(&h, b, blen);
    blake3_hasher_finalize(&h, out32, 32);
}

/* =========================================================
 * Генерация матриц (CPU)
 * Ref: pearl_noisygemm_reference.py / py-pearl-mining
 * ========================================================= */

#define M_DIM  131072
#define N_DIM  131072
#define K_DIM  4096
#define R_RANK 256
#define TM     16
#define TN     16
#define Y_TILES_PER_BATCH 64

/* int32 clamp */
static inline int8_t clamp8(int32_t v) {
    if (v >  127) return  127;
    if (v < -128) return -128;
    return (int8_t)v;
}

static inline uint32_t rotl32(uint32_t x, int s) {
    return (x << s) | (x >> (32 - s));
}

/* Генерируем Ap, Bp (int8) по sigma и difficulty.
 * Ap  [m×k], строчный порядок
 * BpT [n×k], транспонированный Bp (нужен ядру: строка=столбец оригинала)
 *
 * ER и FL — разреженные: один +1 и один -1 на столбец.
 * Используем er_pos/er_neg и fl_pos/fl_neg вместо плотного GEMM:
 *   O(m×k) и O(n×(r+k)) вместо O(m×k×r) = 137 млрд. операций.
 */
static void generate_matrices(const uint8_t* sigma, int sigma_len,
                               double difficulty,
                               int8_t* Ap, int8_t* BpT,
                               uint8_t* sA_out,
                               uint8_t* sB_out,
                               uint8_t* HA_out,
                               uint8_t* HB_out)
{
    const int m = M_DIM, n = N_DIM, k = K_DIM, r = R_RANK;

    printf("[gen] Выделяем A, B (%.1f ГБ)...\n",
           (double)(m*k + n*k) / 1e9);
    fflush(stdout);

    int8_t* A = (int8_t*)malloc((size_t)m * k);
    int8_t* B = (int8_t*)malloc((size_t)n * k);
    if (!A || !B) { fprintf(stderr,"OOM A/B\n"); exit(1); }

    /* Генерируем A и B чанками 4MB — избегаем пика 536MB raw-буфера */
    #define XOF_CHUNK (4*1024*1024)
    {
        uint8_t chunk[XOF_CHUNK];
        size_t sz = (size_t)m * k;
        blake3_hasher h;
        blake3_hasher_init(&h);
        blake3_hasher_update(&h, "matrix_A", 8);
        blake3_hasher_update(&h, sigma, sigma_len);
        for (size_t off = 0; off < sz; off += XOF_CHUNK) {
            size_t n2 = (off + XOF_CHUNK <= sz) ? XOF_CHUNK : sz - off;
            blake3_hasher_finalize_seek(&h, off, chunk, n2);
            for (size_t x = 0; x < n2; x++) A[off+x] = (int8_t)((chunk[x] % 128) - 64);
        }
        printf("[gen] A готово\n"); fflush(stdout);
    }
    {
        uint8_t chunk[XOF_CHUNK];
        size_t sz = (size_t)n * k;
        blake3_hasher h;
        blake3_hasher_init(&h);
        blake3_hasher_update(&h, "matrix_B", 8);
        blake3_hasher_update(&h, sigma, sigma_len);
        for (size_t off = 0; off < sz; off += XOF_CHUNK) {
            size_t n2 = (off + XOF_CHUNK <= sz) ? XOF_CHUNK : sz - off;
            blake3_hasher_finalize_seek(&h, off, chunk, n2);
            for (size_t x = 0; x < n2; x++) B[off+x] = (int8_t)((chunk[x] % 128) - 64);
        }
        printf("[gen] B готово\n"); fflush(stdout);
    }

    /* mu_bytes: struct.pack("<7q d", m, n, k, r, tm, tn, 0, difficulty) */
    uint8_t mu_bytes[64];
    {
        int64_t* p = (int64_t*)mu_bytes;
        p[0]=m; p[1]=n; p[2]=k; p[3]=r; p[4]=TM; p[5]=TN; p[6]=0;
        memcpy(mu_bytes+56, &difficulty, 8);
    }
    uint8_t kappa[32];
    blake3_concat(sigma, sigma_len, mu_bytes, 64, kappa);

    blake3_keyed_hash(kappa, (const uint8_t*)A, (size_t)m*k, HA_out);
    blake3_keyed_hash(kappa, (const uint8_t*)B, (size_t)n*k, HB_out);

    blake3_concat(kappa, 32, HB_out, 32, sB_out);
    blake3_concat(sB_out, 32, HA_out, 32, sA_out);
    printf("[gen] kappa/sA/sB OK\n"); fflush(stdout);

    /* EL[m×r]: uniform [-32,31], keyed by sA */
    int8_t* EL = (int8_t*)malloc((size_t)m * r);
    if (!EL) { fprintf(stderr,"OOM EL\n"); exit(1); }
    {
        uint8_t* raw = (uint8_t*)malloc((size_t)m * r);
        if (!raw) { fprintf(stderr,"OOM EL raw\n"); exit(1); }
        blake3_xof((const uint8_t*)"EL", 2, sA_out, 32, raw, (size_t)m*r);
        for (size_t x = 0; x < (size_t)m*r; x++) EL[x] = (int8_t)((raw[x] % 64) - 32);
        free(raw);
    }

    /* ER: разреженный — один +1 (er_pos[d]) и один -1 (er_neg[d]) на столбец d */
    int* er_pos = (int*)malloc(k * sizeof(int));
    int* er_neg = (int*)malloc(k * sizeof(int));
    if (!er_pos || !er_neg) { fprintf(stderr,"OOM er\n"); exit(1); }
    {
        uint8_t* raw = (uint8_t*)malloc((size_t)k * 8);
        if (!raw) { fprintf(stderr,"OOM ER raw\n"); exit(1); }
        blake3_xof((const uint8_t*)"ER", 2, sA_out, 32, raw, (size_t)k*8);
        for (int c = 0; c < k; c++) {
            int a = raw[c*8] % r;
            int d2 = raw[c*8+1] % r;
            if (d2 == a) d2 = (d2 + 1) % r;
            er_pos[c] = a;
            er_neg[c] = d2;
        }
        free(raw);
    }

    /* FL: разреженный — один +1 (fl_pos[rr]) и один -1 (fl_neg[rr]) на столбец rr */
    int* fl_pos = (int*)malloc(r * sizeof(int));
    int* fl_neg = (int*)malloc(r * sizeof(int));
    if (!fl_pos || !fl_neg) { fprintf(stderr,"OOM fl\n"); exit(1); }
    {
        uint8_t* raw = (uint8_t*)malloc((size_t)r * 8);
        if (!raw) { fprintf(stderr,"OOM FL raw\n"); exit(1); }
        blake3_xof((const uint8_t*)"FL", 2, sB_out, 32, raw, (size_t)r*8);
        for (int c = 0; c < r; c++) {
            int a = raw[c*8] % k;
            int d2 = raw[c*8+1] % k;
            if (d2 == a) d2 = (d2 + 1) % k;
            fl_pos[c] = a;
            fl_neg[c] = d2;
        }
        free(raw);
    }

    /* FRT[n×r]: FR[r×n] транспонированный для кеш-дружелюбного доступа */
    int8_t* FRT = (int8_t*)malloc((size_t)n * r);
    if (!FRT) { fprintf(stderr,"OOM FRT\n"); exit(1); }
    {
        uint8_t* raw = (uint8_t*)malloc((size_t)r * n);
        if (!raw) { fprintf(stderr,"OOM FR raw\n"); exit(1); }
        blake3_xof((const uint8_t*)"FR", 2, sB_out, 32, raw, (size_t)r*n);
        /* Транспонируем: FRT[j*r + rr] = FR_orig[rr*n + j] */
        for (int rr = 0; rr < r; rr++) {
            const uint8_t* src = raw + (size_t)rr * n;
            for (int j = 0; j < n; j++)
                FRT[(size_t)j*r + rr] = (int8_t)((src[j] % 64) - 32);
        }
        free(raw);
        printf("[gen] FRT готово\n"); fflush(stdout);
    }

    printf("[gen] Ap = clip(A + EL@ER) [разреженно, O(m*k)]...\n");
    fflush(stdout);

    /* EL@ER: для каждой строки i и столбца d:
       E[i][d] = EL[i][er_pos[d]] - EL[i][er_neg[d]]   — без вложенного r-цикла */
    #pragma omp parallel for schedule(static)
    for (int i = 0; i < m; i++) {
        const int8_t* eli = EL + (size_t)i * r;
        const int8_t* ai  = A  + (size_t)i * k;
        int8_t*       api = Ap + (size_t)i * k;
        for (int d = 0; d < k; d++) {
            int32_t e = (int32_t)eli[er_pos[d]] - (int32_t)eli[er_neg[d]];
            api[d] = clamp8((int32_t)ai[d] + e);
        }
    }
    free(A); free(EL); free(er_pos); free(er_neg);
    printf("[gen] Ap готово\n"); fflush(stdout);

    printf("[gen] BpT = clip(B + FL@FR) [разреженно, O(n*(r+k))]...\n");
    fflush(stdout);

    /* FL@FR scatter: для каждой строки j в BpT:
       F_row[k] = сумма по rr: FL_col_rr[fl_pos[rr]] * FRT[j][rr]
                               FL_col_rr[fl_neg[rr]] * (-FRT[j][rr])
       FL-столбец rr имеет только +1 в fl_pos[rr] и -1 в fl_neg[rr]. */
    #pragma omp parallel for schedule(static)
    for (int j = 0; j < n; j++) {
        int32_t F_row[K_DIM]; /* 16 KB на стеке потока */
        memset(F_row, 0, sizeof(F_row));
        const int8_t* frt_j = FRT + (size_t)j * r;
        for (int rr = 0; rr < r; rr++) {
            int32_t v = (int32_t)frt_j[rr];
            F_row[fl_pos[rr]] += v;
            F_row[fl_neg[rr]] -= v;
        }
        const int8_t* bj  = B   + (size_t)j * k;
        int8_t*       bpj = BpT + (size_t)j * k;
        for (int d = 0; d < k; d++)
            bpj[d] = clamp8((int32_t)bj[d] + F_row[d]);
    }
    free(B); free(FRT); free(fl_pos); free(fl_neg);
    printf("[gen] BpT готово\n"); fflush(stdout);
}

/* =========================================================
 * CUDA ядро: tiled NoisyGEMM → transcript → проверка target
 *
 * Ap  [M×K] int8 row-major
 * BpT [N×K] int8 row-major (транспонированный Bp)
 * sA_key32[8] = uint32 LE слова 32-байтного ключа sA
 *
 * Каждый блок: один тайл (bi, bj), tm×tn потоков
 * ========================================================= */

__device__ __forceinline__ uint32_t d_rotl32(uint32_t x, int s){
    return (x<<s)|(x>>(32-s));
}

/* BLAKE3 compression (device) */
__device__ __constant__ uint32_t B3IV[8] = {
    0x6A09E667u, 0xBB67AE85u, 0x3C6EF372u, 0xA54FF53Au,
    0x510E527Fu, 0x9B05688Cu, 0x1F83D9ABu, 0x5BE0CD19u
};

__device__ __forceinline__ uint32_t d_rotr32(uint32_t x, int n){
    return (x >> n) | (x << (32 - n));
}
__device__ __forceinline__ void b3G(uint32_t* v,int a,int b,int c,int d,uint32_t x,uint32_t y){
    v[a]+=v[b]+x; v[d]=d_rotr32(v[d]^v[a],16);
    v[c]+=v[d];   v[b]=d_rotr32(v[b]^v[c],12);
    v[a]+=v[b]+y; v[d]=d_rotr32(v[d]^v[a], 8);
    v[c]+=v[d];   v[b]=d_rotr32(v[b]^v[c], 7);
}

__device__ void b3_compress64(const uint32_t* key8, const uint32_t* msg16,
                               uint32_t* out8)
{
    /* flags = CHUNK_START|CHUNK_END|ROOT|KEYED_HASH = 0x1B */
    uint32_t v[16]={
        key8[0],key8[1],key8[2],key8[3],
        key8[4],key8[5],key8[6],key8[7],
        B3IV[0],B3IV[1],B3IV[2],B3IV[3],
        0,0,64u,0x1Bu
    };
    uint32_t m[16];
    for(int i=0;i<16;i++) m[i]=msg16[i];

    /* 7 раундов; после каждого — перестановка MSG_PERMUTATION */
    const uint8_t PERM[16]={2,6,3,10,7,0,4,13,1,11,12,5,9,14,15,8};
    for(int round=0;round<7;round++){
        b3G(v,0,4, 8,12,m[0], m[1]);
        b3G(v,1,5, 9,13,m[2], m[3]);
        b3G(v,2,6,10,14,m[4], m[5]);
        b3G(v,3,7,11,15,m[6], m[7]);
        b3G(v,0,5,10,15,m[8], m[9]);
        b3G(v,1,6,11,12,m[10],m[11]);
        b3G(v,2,7, 8,13,m[12],m[13]);
        b3G(v,3,4, 9,14,m[14],m[15]);
        if(round<6){
            uint32_t t[16];
            for(int i=0;i<16;i++) t[i]=m[PERM[i]];
            for(int i=0;i<16;i++) m[i]=t[i];
        }
    }
    for(int i=0;i<8;i++) out8[i]=v[i]^v[i+8];
}

/* Ядро майнинга */
__global__ void mine_kernel(
    const int8_t* __restrict__ Ap,   /* [M×K] */
    const int8_t* __restrict__ BpT,  /* [N×K] транспонированный */
    int M, int N, int K, int R,
    const uint32_t* __restrict__ sA32, /* 8 uint32 */
    /* target: 8 uint32 LE (256-бит) */
    uint32_t t0,uint32_t t1,uint32_t t2,uint32_t t3,
    uint32_t t4,uint32_t t5,uint32_t t6,uint32_t t7,
    int bj_tile_base,
    /* выход */
    int* out_i, int* out_j, uint32_t* out_digest8, uint32_t* out_transcript16,
    int* found_flag
){
    const int ti = (int)blockIdx.x * TM;
    const int tj = ((int)blockIdx.y + bj_tile_base) * TN;
    if(ti+TM>M || tj+TN>N) return;

    const int row = threadIdx.y; /* 0..TM-1 */
    const int col = threadIdx.x; /* 0..TN-1 */

    /* Shared: части sub-матрицы для XOR-редукции */
    __shared__ uint32_t sX[TM*TN]; /* один элемент на поток */
    __shared__ uint32_t sM[16];    /* transcript */
    if(threadIdx.x==0 && threadIdx.y==0)
        for(int i=0;i<16;i++) sM[i]=0u;
    __syncthreads();

    /* Цикл по r-блокам */
    int l = 0;
    for(int s=0; s+R<=K; s+=R, l++){
        /* Dot product Ap[ti+row, s:s+R] · BpT[tj+col, s:s+R] */
        int32_t acc=0;
        int s_end = s+R;
        for(int d=s; d<s_end; d+=4){
            /* DP4A: 4 int8 за раз */
            int va, vb;
            const int8_t* ap = Ap + (ti+row)*K + d;
            const int8_t* bp = BpT+ (tj+col)*K + d;
            memcpy(&va, ap, 4);
            memcpy(&vb, bp, 4);
            acc = __dp4a(va, vb, acc);
        }
        /* Каждый поток пишет свой acc как uint32 */
        sX[threadIdx.y*TN + threadIdx.x] = (uint32_t)acc;
        __syncthreads();

        /* XOR-редукция 256→1, только thread(0,0) */
        if(threadIdx.x==0 && threadIdx.y==0){
            uint32_t X=0;
            for(int i=0;i<TM*TN;i++) X ^= sX[i];
            uint32_t idx = (uint32_t)(l % 16);
            sM[idx] = d_rotl32(sM[idx],13) ^ X;
        }
        __syncthreads();
    }

    /* Только один поток выполняет BLAKE3 и проверку */
    if(threadIdx.x==0 && threadIdx.y==0){
        /* BLAKE3(sM[0..15], key=sA) */
        uint32_t digest[8];
        b3_compress64(sA32, sM, digest);

        /* Сравнение uint256 LE: digest <= target */
        bool ok = false;
        uint32_t tgt[8]={t0,t1,t2,t3,t4,t5,t6,t7};
        for(int w=7;w>=0;w--){
            if(digest[w] < tgt[w]){ ok=true; break; }
            if(digest[w] > tgt[w]){ ok=false; break; }
            if(w==0) ok=true; /* равны */
        }

        if(ok){
            if(atomicCAS(found_flag,0,1)==0){
                *out_i = ti;
                *out_j = tj;
                for(int w=0;w<8;w++) out_digest8[w]=digest[w];
                for(int w=0;w<16;w++) out_transcript16[w]=sM[w];
            }
        }
    }
}

/* =========================================================
 * Stratum клиент (TCP + JSON)
 * ========================================================= */

static int tcp_sock = -1;
static char g_seed[128]  = {0};
static double g_diff      = 32.0;
static volatile int g_new_job = 0;
static pthread_mutex_t g_mtx = PTHREAD_MUTEX_INITIALIZER;
static int g_submit_mode = 0;      /* 0=obj-full, 1=obj-min, 2=array-min */
static int g_submit_inflight = 0;  /* ждём result/error после submit */
static char g_last_share_id[256] = {0}; /* seed:tile_i:tile_j:digest */

static int tcp_connect(const char* host, int port){
    struct hostent* he = gethostbyname(host);
    if(!he){ perror("gethostbyname"); return -1; }
    int s = socket(AF_INET,SOCK_STREAM,0);
    struct sockaddr_in sa;
    sa.sin_family=AF_INET;
    sa.sin_port=htons(port);
    memcpy(&sa.sin_addr, he->h_addr_list[0], he->h_length);
    if(connect(s,(struct sockaddr*)&sa,sizeof(sa))<0){ perror("connect"); close(s); return -1; }
    return s;
}

static int send_json(int sock, const char* json){
    char buf[4096];
    snprintf(buf,sizeof(buf),"%s\n",json);
    ssize_t n = send(sock,buf,strlen(buf),0);
    if(n < 0){
        perror("send");
        return 0;
    }
    return 1;
}

/* Минимальный JSON-поиск строкового значения ключа */
static int json_str(const char* json, const char* key, char* out, int outlen){
    char pat[128]; snprintf(pat,sizeof(pat),"\"%s\"",key);
    const char* p = strstr(json,pat); if(!p) return 0;
    p += strlen(pat);
    while(*p==' '||*p==':') p++;
    if(*p!='"') return 0; p++;
    int i=0;
    while(*p && *p!='"' && i<outlen-1) out[i++]=*p++;
    out[i]=0; return 1;
}

static double json_num(const char* json, const char* key){
    char pat[128]; snprintf(pat,sizeof(pat),"\"%s\"",key);
    const char* p = strstr(json,pat); if(!p) return 0;
    p += strlen(pat);
    while(*p==' '||*p==':') p++;
    return atof(p);
}

static char net_buf[65536];
static int  net_pos=0;

static void bin_to_hex(const uint8_t* in, size_t n, char* out){
    for(size_t i = 0; i < n; i++) sprintf(out + i*2, "%02x", in[i]);
    out[n*2] = 0;
}

static int send_submit_with_mode(
    int sock, int msg_id, int mode,
    const char* seed, int tile_i, int tile_j, const char* digest,
    const char* sA_hex, const char* sB_hex, const char* HA_hex,
    const char* HB_hex, const char* transcript
){
    char sub[4096];
    if(mode == 0){
        snprintf(sub,sizeof(sub),
            "{\"id\":%d,\"method\":\"pearl.submit\","
            "\"params\":{\"seed\":\"%s\",\"tile_i\":%d,\"tile_j\":%d,"
            "\"sA\":\"%s\",\"sB\":\"%s\",\"HA\":\"%s\",\"HB\":\"%s\","
            "\"transcript\":\"%s\",\"digest\":\"%s\"}}",
            msg_id, seed, tile_i, tile_j, sA_hex, sB_hex, HA_hex, HB_hex, transcript, digest);
    } else if(mode == 1){
        snprintf(sub,sizeof(sub),
            "{\"id\":%d,\"method\":\"pearl.submit\","
            "\"params\":{\"seed\":\"%s\",\"tile_i\":%d,\"tile_j\":%d,\"digest\":\"%s\"}}",
            msg_id, seed, tile_i, tile_j, digest);
    } else {
        snprintf(sub,sizeof(sub),
            "{\"id\":%d,\"method\":\"pearl.submit\","
            "\"params\":[\"%s\",%d,%d,\"%s\"]}",
            msg_id, seed, tile_i, tile_j, digest);
    }
    printf("[net] submit mode=%d json=%s\n", mode, sub); fflush(stdout);
    return send_json(sock, sub);
}

static char* read_line(int sock){
    while(1){
        char* nl = (char*)memchr(net_buf,'\n',net_pos);
        if(nl){
            *nl=0;
            int len = (int)(nl - net_buf)+1;
            memmove(net_buf, nl+1, net_pos - len);
            net_pos -= len;
            return net_buf; /* ОСТОРОЖНО: перезапишется при следующем вызове */
        }
        if(net_pos >= (int)sizeof(net_buf)-1) net_pos=0;
        int n = recv(sock, net_buf+net_pos, sizeof(net_buf)-net_pos-1, 0);
        if(n<=0) return NULL;
        net_pos += n;
    }
}

/* =========================================================
 * GPU контекст — мульти-GPU
 * ========================================================= */

#define CU_CHECK(call) do { \
    cudaError_t _e = (call); \
    if(_e != cudaSuccess){ \
        fprintf(stderr,"[CUDA] %s:%d %s: %s\n",__FILE__,__LINE__,#call,cudaGetErrorString(_e)); \
        exit(1); \
    } \
} while(0)

#define MAX_GPUS 16

typedef struct {
    int          dev;
    int8_t*      d_Ap;
    int8_t*      d_BpT;
    int*         d_found;
    int*         d_out_i;
    int*         d_out_j;
    uint32_t*    d_out_digest;
    uint32_t*    d_out_transcript;
    uint32_t*    d_sA32;
} GpuCtx;

static GpuCtx g_gpus[MAX_GPUS];
static int    g_ngpu = 0;

static void gpu_init_all(int* devs, int ndev){
    size_t szAp  = (size_t)M_DIM * K_DIM;
    size_t szBpT = (size_t)N_DIM * K_DIM;
    g_ngpu = ndev;
    printf("[gpu] Инициализируем %d GPU (Ap=%.0fMB BpT=%.0fMB каждая)...\n",
           ndev, (double)szAp/1e6, (double)szBpT/1e6);
    fflush(stdout);
    for(int i = 0; i < ndev; i++){
        GpuCtx* g = &g_gpus[i];
        g->dev = devs[i];
        CU_CHECK(cudaSetDevice(g->dev));
        CU_CHECK(cudaMalloc(&g->d_Ap,         szAp));
        CU_CHECK(cudaMalloc(&g->d_BpT,        szBpT));
        CU_CHECK(cudaMalloc(&g->d_found,      sizeof(int)));
        CU_CHECK(cudaMalloc(&g->d_out_i,      sizeof(int)));
        CU_CHECK(cudaMalloc(&g->d_out_j,      sizeof(int)));
        CU_CHECK(cudaMalloc(&g->d_out_digest, 8*sizeof(uint32_t)));
        CU_CHECK(cudaMalloc(&g->d_out_transcript, 16*sizeof(uint32_t)));
        CU_CHECK(cudaMalloc(&g->d_sA32,       8*sizeof(uint32_t)));
        printf("[gpu] GPU%d OK\n", g->dev); fflush(stdout);
    }
}

static int gpu_mine_all(const int8_t* h_Ap, const int8_t* h_BpT,
                        const uint8_t* sA, double difficulty,
                        int* found_i, int* found_j, char* digest_hex, char* transcript_hex)
{
    size_t szAp  = (size_t)M_DIM * K_DIM;
    size_t szBpT = (size_t)N_DIM * K_DIM;

    uint32_t sA32[8];
    memcpy(sA32, sA, 32);

    long double exp_val = 256.0L - (long double)difficulty + log2l((long double)(R_RANK * TM * TN));
    uint32_t tgt[8]={0};
    if(exp_val >= 256.0L){
        for(int i=0;i<8;i++) tgt[i]=0xFFFFFFFFu;
    } else if(exp_val > 0.0L){
        long double v = powl(2.0L, exp_val);
        if(!isfinite((double)v)){
            for(int i=0;i<8;i++) tgt[i]=0xFFFFFFFFu;
        } else {
            long double base = 4294967296.0L; /* 2^32 */
            for(int i=0;i<8;i++){
                long double rem = fmodl(v, base);
                if(rem < 0.0L) rem = 0.0L;
                if(rem > 4294967295.0L) rem = 4294967295.0L;
                tgt[i] = (uint32_t)rem;
                v = floorl(v / base);
                if(v <= 0.0L) break;
            }
        }
    }
    printf("[gpu] target LE words: %08X %08X %08X %08X %08X %08X %08X %08X\n",
           tgt[0],tgt[1],tgt[2],tgt[3],tgt[4],tgt[5],tgt[6],tgt[7]);
    printf("[gpu] запускаем %d GPU...\n", g_ngpu);
    fflush(stdout);

    dim3 block(TN, TM);
    const int x_tiles = M_DIM / TM;
    const int y_tiles = N_DIM / TN;
    const int y_batch = Y_TILES_PER_BATCH;
    int zero = 0;

    /* Копируем вход на каждую GPU */
    for(int i = 0; i < g_ngpu; i++){
        GpuCtx* g = &g_gpus[i];
        CU_CHECK(cudaSetDevice(g->dev));
        CU_CHECK(cudaMemcpy(g->d_Ap,    h_Ap,  szAp,  cudaMemcpyHostToDevice));
        CU_CHECK(cudaMemcpy(g->d_BpT,   h_BpT, szBpT, cudaMemcpyHostToDevice));
        CU_CHECK(cudaMemcpy(g->d_sA32,  sA32,  32,    cudaMemcpyHostToDevice));
    }

    for(int i = 0; i < g_ngpu; i++){
        GpuCtx* g = &g_gpus[i];
        CU_CHECK(cudaSetDevice(g->dev));
        CU_CHECK(cudaMemcpy(g->d_found, &zero, sizeof(int), cudaMemcpyHostToDevice));
    }

    /* Батчевый запуск по оси Y для более быстрой реакции и ранней остановки */
    int found = 0;
    for(int y0 = 0; y0 < y_tiles && !found; y0 += y_batch){
        int gy = y_batch;
        if(y0 + gy > y_tiles) gy = y_tiles - y0;
        dim3 grid(x_tiles, gy);

        for(int i = 0; i < g_ngpu; i++){
            GpuCtx* g = &g_gpus[i];
            CU_CHECK(cudaSetDevice(g->dev));
            mine_kernel<<<grid,block>>>(
                g->d_Ap, g->d_BpT, M_DIM, N_DIM, K_DIM, R_RANK, g->d_sA32,
                tgt[0],tgt[1],tgt[2],tgt[3],tgt[4],tgt[5],tgt[6],tgt[7],
                y0,
                g->d_out_i, g->d_out_j, g->d_out_digest, g->d_out_transcript, g->d_found
            );
            CU_CHECK(cudaGetLastError());
        }

        for(int i = 0; i < g_ngpu; i++){
            GpuCtx* g = &g_gpus[i];
            CU_CHECK(cudaSetDevice(g->dev));
            CU_CHECK(cudaDeviceSynchronize());
            int f = 0;
            CU_CHECK(cudaMemcpy(&f, g->d_found, sizeof(int), cudaMemcpyDeviceToHost));
            if(f && !found){
                found = 1;
                cudaMemcpy(found_i, g->d_out_i, sizeof(int), cudaMemcpyDeviceToHost);
                cudaMemcpy(found_j, g->d_out_j, sizeof(int), cudaMemcpyDeviceToHost);
                uint32_t dg[8];
                cudaMemcpy(dg, g->d_out_digest, 32, cudaMemcpyDeviceToHost);
                uint8_t* b = (uint8_t*)dg;
                for(int k=0;k<32;k++) sprintf(digest_hex+k*2,"%02x",b[k]);
                digest_hex[64]=0;
                uint32_t tr[16];
                cudaMemcpy(tr, g->d_out_transcript, 64, cudaMemcpyDeviceToHost);
                uint8_t* tb = (uint8_t*)tr;
                for(int k=0;k<64;k++) sprintf(transcript_hex+k*2,"%02x",tb[k]);
                transcript_hex[128]=0;
                printf("[gpu] GPU%d: НАШЁЛ ШАРУ!\n", g->dev); fflush(stdout);
            }
        }
        printf("[gpu] прогресс: y-tiles %d/%d\n", y0 + gy, y_tiles);
        fflush(stdout);
    }
    printf("[gpu] Все GPU завершили\n"); fflush(stdout);
    return found;
}

/* =========================================================
 * main
 * ========================================================= */

int main(int argc, char** argv){
    const char* pool_host = "pearl.baikalmine.com";
    int         pool_port = 2010;
    const char* wallet    = NULL;
    int         devs[MAX_GPUS] = {0};
    int         ndev = 0;

    for(int i=1;i<argc;i++){
        if(!strcmp(argv[i],"--pool") && i+1<argc){
            const char* u = argv[++i];
            const char* h = strstr(u,"://");
            if(h){ h+=3;
                const char* colon = strchr(h,':');
                if(colon){
                    int hlen = (int)(colon-h);
                    static char hbuf[256];
                    strncpy(hbuf,h,hlen); hbuf[hlen]=0;
                    pool_host=hbuf; pool_port=atoi(colon+1);
                }
            }
        } else if(!strcmp(argv[i],"--wallet") && i+1<argc) wallet=argv[++i];
        else if((!strcmp(argv[i],"--device")||!strcmp(argv[i],"--devices")) && i+1<argc){
            /* "0" или "0,1,2,3" */
            const char* s = argv[++i];
            char tmp[256]; strncpy(tmp,s,255); tmp[255]=0;
            char* tok = strtok(tmp,",");
            while(tok && ndev<MAX_GPUS){ devs[ndev++]=atoi(tok); tok=strtok(NULL,","); }
        }
        else if(!strcmp(argv[i],"--help")||!strcmp(argv[i],"-h")){
            printf("Pearl NoisyGEMM miner (sm_75/sm_86)\n");
            printf("  --pool URI       stratum+tcp://host:port\n");
            printf("  --wallet ADDR    wallet.worker\n");
            printf("  --devices N[,M]  CUDA устройства (default: 0)\n");
            return 0;
        }
    }
    if(!wallet){ fprintf(stderr,"--wallet required\n"); return 1; }
    if(!ndev){ devs[0]=0; ndev=1; }

    gpu_init_all(devs, ndev);

    int8_t* h_Ap  = (int8_t*)malloc((size_t)M_DIM * K_DIM);
    int8_t* h_BpT = (int8_t*)malloc((size_t)N_DIM * K_DIM);
    if(!h_Ap || !h_BpT){ fprintf(stderr,"OOM host matrices\n"); return 1; }

    uint8_t sA[32], sB[32], HA[32], HB[32];
    char cur_seed[128]={0};
    int msg_id = 3;

reconnect:
    if(tcp_sock >= 0){ close(tcp_sock); tcp_sock=-1; }
    if(g_submit_inflight){
        g_submit_mode = (g_submit_mode + 1) % 3;
        g_submit_inflight = 0;
        printf("[net] submit оборван; переключаю mode на %d\n", g_submit_mode);
        fflush(stdout);
    }
    net_pos = 0;
    printf("[main] Подключаемся к %s:%d...\n",pool_host,pool_port);
    while(1){
        tcp_sock = tcp_connect(pool_host, pool_port);
        if(tcp_sock >= 0) break;
        printf("[main] Переподключение через 5 сек...\n"); fflush(stdout);
        sleep(5);
    }

    /* Handshake */
    {
        char msg[512];
        snprintf(msg,sizeof(msg),
            "{\"id\":1,\"method\":\"mining.subscribe\",\"params\":[\"pearl-cu/1.0\"]}");
        if(!send_json(tcp_sock, msg)) goto reconnect;
        snprintf(msg,sizeof(msg),
            "{\"id\":2,\"method\":\"mining.authorize\",\"params\":[\"%s\",\"x\"]}",wallet);
        if(!send_json(tcp_sock, msg)) goto reconnect;
    }

    while(1){
        char* line = read_line(tcp_sock);
        if(!line){
            printf("[net] Соединение потеряно, переподключаемся...\n"); fflush(stdout);
            goto reconnect;
        }

        if(strstr(line,"pearl.challenge")){
            char seed[128]={0}; double diff=32.0;
            json_str(line,"seed",seed,sizeof(seed));
            diff = json_num(line,"difficulty");
            if(!diff) diff=32.0;

            if(!strcmp(seed,cur_seed)){ continue; }
            strncpy(cur_seed,seed,sizeof(cur_seed)-1);

            printf("[job] seed=%s diff=%.1f\n",seed,diff); fflush(stdout);

            int slen = strlen(seed)/2;
            uint8_t* sigma = (uint8_t*)malloc(slen);
            for(int i=0;i<slen;i++){
                unsigned v; sscanf(seed+i*2,"%02x",&v); sigma[i]=(uint8_t)v;
            }

            printf("[gen] Генерируем матрицы (diff=%.1f)...\n", diff); fflush(stdout);
            generate_matrices(sigma, slen, diff, h_Ap, h_BpT, sA, sB, HA, HB);
            free(sigma);

            printf("[gpu] Запускаем ядра...\n"); fflush(stdout);
            int fi=-1, fj=-1; char dg[65]={0}; char transcript[129]={0};
            int found = gpu_mine_all(h_Ap, h_BpT, sA, diff, &fi, &fj, dg, transcript);

            if(found){
                char sA_hex[65], sB_hex[65], HA_hex[65], HB_hex[65];
                bin_to_hex(sA, 32, sA_hex);
                bin_to_hex(sB, 32, sB_hex);
                bin_to_hex(HA, 32, HA_hex);
                bin_to_hex(HB, 32, HB_hex);
                printf("[gpu] НАЙДЕНО тайл(%d,%d) digest=%s\n",fi,fj,dg); fflush(stdout);
                char share_id[256];
                snprintf(share_id, sizeof(share_id), "%s:%d:%d:%s", cur_seed, fi, fj, dg);
                if(!strcmp(share_id, g_last_share_id)){
                    printf("[net] duplicate share, skip submit\n"); fflush(stdout);
                    continue;
                }
                strncpy(g_last_share_id, share_id, sizeof(g_last_share_id)-1);
                g_last_share_id[sizeof(g_last_share_id)-1] = 0;
                if(!send_submit_with_mode(
                    tcp_sock, msg_id++, g_submit_mode, cur_seed, fi, fj, dg,
                    sA_hex, sB_hex, HA_hex, HB_hex, transcript
                )){
                    printf("[net] send submit failed\n"); fflush(stdout);
                    goto reconnect;
                }
                g_submit_inflight = 1;
                printf("[net] submit отправлен, ждём ответ пула...\n"); fflush(stdout);
            } else {
                printf("[gpu] Тайл не найден\n"); fflush(stdout);
            }
        } else if(strstr(line,"result") || strstr(line,"error")){
            printf("[pool] %s\n", line); fflush(stdout);
            g_submit_inflight = 0;
        }
    }

    free(h_Ap); free(h_BpT);
    return 0;
}
