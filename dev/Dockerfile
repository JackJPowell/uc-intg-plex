FROM python:3.11-slim-bullseye
WORKDIR /app
RUN apt-get update && apt-get install -y npm && npm install -g nodemon
COPY ./requirements.txt requirements.txt
RUN pip3 install --no-cache-dir --upgrade -r requirements.txt
ADD . .
CMD ["python3", "-u", "intg-jvc/driver.py"]