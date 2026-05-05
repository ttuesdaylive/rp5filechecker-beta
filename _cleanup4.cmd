@echo off
cd /d "%~dp0"
echo === run start === > "C:\Users\jakec\Documents\Python Projects (GRAVEYARD BFFR)\rp5filechecker\_log_temp.txt"
echo cwd: %CD% >> "C:\Users\jakec\Documents\Python Projects (GRAVEYARD BFFR)\rp5filechecker\_log_temp.txt"
git status --short >> "C:\Users\jakec\Documents\Python Projects (GRAVEYARD BFFR)\rp5filechecker\_log_temp.txt" 2>&1
git reset >> "C:\Users\jakec\Documents\Python Projects (GRAVEYARD BFFR)\rp5filechecker\_log_temp.txt" 2>&1
git add -A >> "C:\Users\jakec\Documents\Python Projects (GRAVEYARD BFFR)\rp5filechecker\_log_temp.txt" 2>&1
echo --- after add --- >> "C:\Users\jakec\Documents\Python Projects (GRAVEYARD BFFR)\rp5filechecker\_log_temp.txt"
git status --short >> "C:\Users\jakec\Documents\Python Projects (GRAVEYARD BFFR)\rp5filechecker\_log_temp.txt" 2>&1
git commit -m "Remove stray helper files" >> "C:\Users\jakec\Documents\Python Projects (GRAVEYARD BFFR)\rp5filechecker\_log_temp.txt" 2>&1
git branch -f beta HEAD >> "C:\Users\jakec\Documents\Python Projects (GRAVEYARD BFFR)\rp5filechecker\_log_temp.txt" 2>&1
git tag -f beta HEAD >> "C:\Users\jakec\Documents\Python Projects (GRAVEYARD BFFR)\rp5filechecker\_log_temp.txt" 2>&1
git push origin HEAD:main >> "C:\Users\jakec\Documents\Python Projects (GRAVEYARD BFFR)\rp5filechecker\_log_temp.txt" 2>&1
git push origin --force refs/heads/beta:refs/heads/beta >> "C:\Users\jakec\Documents\Python Projects (GRAVEYARD BFFR)\rp5filechecker\_log_temp.txt" 2>&1
git push origin --force refs/tags/beta >> "C:\Users\jakec\Documents\Python Projects (GRAVEYARD BFFR)\rp5filechecker\_log_temp.txt" 2>&1
echo --- exit code: %ERRORLEVEL% --- >> "C:\Users\jakec\Documents\Python Projects (GRAVEYARD BFFR)\rp5filechecker\_log_temp.txt"
(goto) 2>nul ^& del "%~f0"
