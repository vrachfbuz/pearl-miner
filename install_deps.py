import zipfile, site, os
wheels = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'wheels')
dest = site.getsitepackages()[0]
for f in sorted(os.listdir(wheels)):
    if f.endswith('.whl'):
        print('Устанавливаю:', f)
        with zipfile.ZipFile(os.path.join(wheels, f)) as z:
            z.extractall(dest)
print('Готово!')
