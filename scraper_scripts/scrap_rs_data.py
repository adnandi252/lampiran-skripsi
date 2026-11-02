import requests
import pandas as pd

# URL API
api_url = "https://sirs.kemkes.go.id/fo/home/rekap_rs_all?id=0"

# Header
headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Referer": "https://sirs.kemkes.go.id/fo/home/dashboard_rs",
    "X-Requested-With": "XMLHttpRequest"
}

print("Sedang mengambil data dari API...")
response = requests.get(api_url, headers=headers)

if response.status_code == 200:
    try:
        json_data = response.json()
        
        if isinstance(json_data, dict) and 'data' in json_data:
            tabel_data = json_data['data']
        else:
            tabel_data = json_data
            
        df = pd.DataFrame(tabel_data)
        
        nama_file = "data_rumah_sakit_api.csv"
        df.to_csv(nama_file, index=False, sep=';')
        
        print(f"Scraping API Berhasil! Data telah disimpan di '{nama_file}'")
        print(f"Total data yang ditarik: {len(df)} baris")
        
        print("\nPreview 5 data pertama:")
        print(df.head())
        
    except Exception as e:
        print("Terjadi kesalahan saat memproses data JSON:", e)
        print("Tampilan mentah response (100 karakter pertama):", response.text[:100])
else:
    print(f"Gagal mengambil data. Status code: {response.status_code}")