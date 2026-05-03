$content = Get-Content "C:\Users\markm\Desktop\freelanceos-storefront\_yte_tmp.txt" -Raw
Set-Content -Path "C:\Users\markm\Desktop\freelanceos-storefront\youtube-empire.html" -Value $content -Encoding UTF8
